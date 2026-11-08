#!/usr/bin/env python3
"""autopost.py - worker residente que consume la cola del daemon y publica
cada item en grupos invocando share_post.py como subproceso (CLI intacto).

Contrato externo estable:

  cola     : autoqueue.json        (la escribe SOLO el daemon; este worker es
                                    el unico lector: NUNCA escribe la cola)
  intentos : autopost_attempts.json (archivo PROPIEDAD DEL WORKER, clave =
                                    permalink normalizado; a-lo-mas-una-vez)
  ledger   : ledger.db             (SQLite; UNICO ESCRITOR es este worker:
                                    intencion ANTES del clic y resultado solo
                                    con evidencia de share_post)
  control  : autopost.cmd.json     (se consume y se borra)
  estado   : autopost_status.json   pid: autopost.pid
  CLI      : python3 autopost.py --cmd <cmd-file>

Respeta el kill-switch (/health -> enabled), los intervalos con jitter, los
topes de grupos y diario, la a-lo-mas-una-vez (persistida en el archivo de
intentos, no en la cola) y no roba el foco al usuario.

F1 (contrato congelado, work/f1_interfaces.md §4): la ronda se elige desde el
ledger (relevancia -> elegibilidad -> enfriamiento -> orden last_ts asc), se
pasa la lista de gids a share_post y el resultado se acredita por grupo. Si no
hay elegibles el item se DIFIERE (no muere); nunca se re-publica a ciegas.
"""
import argparse
import calendar
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.request

import cxtlib
import fastmode
import ledger

DIR = cxtlib.resolve_dir()
QUEUE = os.path.join(DIR, "autoqueue.json")
CMD = os.path.join(DIR, "autopost.cmd.json")
STATUS = os.path.join(DIR, "autopost_status.json")
PID = os.path.join(DIR, "autopost.pid")
ATTEMPTS = os.path.join(DIR, "autopost_attempts.json")
SHARE = cxtlib.resolve_script("share_post.py")
# Legado: el daemon sigue usando este path. El worker NUNCA lo toca (§3.4):
# escribe un `share_post.cmd.<batch>.json` propio por ronda.
SHARE_CMD = os.path.join(DIR, "share_post.cmd.json")
SHARE_STATUS = os.path.join(DIR, "share_post_status.json")
# F1: el ledger y la relevancia viven junto al resto del estado.
GROUPS = os.path.join(DIR, "groups.json")
RELEVANCE = os.path.join(DIR, "relevance.json")
LOCK = os.path.join(DIR, "browser.lock")
# Parada dura (§3.7) y lock exclusivo del worker (§3.4).
HALT = os.path.join(DIR, "autopost.halt.json")
WORKER_LOCK = os.path.join(DIR, "autopost.lock")
CXT = cxtlib.resolve_cxt()
PORT = os.environ.get("CXT_PORT") or "8799"

INTERVAL_MIN = float(os.environ.get("CXT_AUTO_MIN", 10))
INTERVAL_REC = float(os.environ.get("CXT_AUTO_REC", 15))
INTERVAL_MAX = float(os.environ.get("CXT_AUTO_MAX", 60))
MAXGROUPS = int(os.environ.get("CXT_AUTO_MAXGROUPS", 10))
# MAXDAY cuenta GRUPOS publicados por dia UTC, no rondas ni items. El jefe fijo
# 60/dia; el tope POR RONDA sigue siendo MAXGROUPS (10). El efectivo es
# min(MAXDAY - hechos_hoy, MAXGROUPS): nunca se pasa del techo de cuenta.
MAXDAY = int(os.environ.get("CXT_AUTO_MAXDAY", 60))
# Alias con el nombre que usa el plan (§1) para el techo de cuenta.
TECHO_CUENTA = MAXDAY
# Una publicacion por grupo y dia (regla dura anti-spam).
MAX_GRUPO_DIA = int(os.environ.get("CXT_AUTO_MAX_GRUPO_DIA", 1))
MAX_GROUPS_PER_DAY = MAX_GRUPO_DIA
# Tras una ronda real, cuantas parejas `unknown` se revisan con la vista member
# antes de dar el item por fallido. Cuesta ~30 s cada una: acotado a 3.
RECONCILE_UNKNOWN_MAX = int(os.environ.get("CXT_AUTO_RECONCILE_MAX", 3))
# Enfriamiento por PUBLICACION (misma publicacion en el mismo grupo) y por
# GRUPO (publicaciones distintas): 6 h cada uno, segun decision del jefe.
COOLDOWN_POST = int(float(os.environ.get("CXT_AUTO_COOLDOWN_H", "6")) * 3600 * 1000)
GAP_GRUPO = int(float(os.environ.get("CXT_AUTO_GAP_GRUPO_H", "6")) * 3600 * 1000)
DEPTH = 200
# TTL del item: 90 dias. Con 7 d un ciclo de cobertura largo expiraba a mitad
# del primer barrido y la cobertura nunca cerraba. El NOMBRE se conserva porque
# otros modulos/tests lo referencian; el valor lo manda el env.
EXPIRE_MS = int(float(os.environ.get("CXT_AUTO_EXPIRE_DAYS", "90")) * 24 * 3600 * 1000)
JITTER = 0.30
# Sondeo CORTO del bucle: el worker debe reaccionar a un enqueue / ACELERAR /
# reintentar / borrar en < 0.5 s, no esperar 2 s al proximo tick (banda pedida
# 200-300 ms). El camino caro (Chrome/daemon) va throttled para no girar CPU.
LOOP_SLEEP = float(os.environ.get("CXT_AUTO_LOOP", "0.25"))
# Cada cuanto se corren los chequeos CAROS (health, tab, checkpoint) con la
# cola ociosa: no spamea el daemon/Chrome a 4 Hz. 1 s.
HEAVY_MS = int(os.environ.get("CXT_AUTO_HEAVY_MS", "1000"))
# Presupuesto de share POR GRUPO. Antes la cota era GLOBAL y unica (420 s): un
# job de 10 grupos moria en el grupo 5. Ahora el tope TOTAL del subproceso =
# GROUP_TIMEOUT * grupos + SHARE_MARGIN, recortado a [MIN, MAX]. CXT_AUTO_SHARE
# _TIMEOUT sigue funcionando, ahora como presupuesto POR GRUPO (default 120 s).
GROUP_TIMEOUT = int(os.environ.get("CXT_AUTO_SHARE_TIMEOUT", "120"))
SHARE_MARGIN = int(os.environ.get("CXT_AUTO_SHARE_MARGIN", "60"))
SHARE_TIMEOUT_MIN = int(os.environ.get("CXT_AUTO_SHARE_TIMEOUT_MIN", "120"))
# P7: el peor caso del hijo es 9*360 + 10*120 = 4440 s (pausas mas trabajo);
# el presupuesto NUNCA puede bajar de 4800 s o una ronda sana muere a mitad y
# deja filas inflight que no hacian falta.
SHARE_TIMEOUT_FLOOR = 4800
# P7: un kick/enqueue acelera la proxima ronda pero sin colapsar la pausa
# anti-ban de 4-8 min: el suelo es ahora + 60 s, nunca 0.
KICK_MIN_MS = 60_000
# El presupuesto total incluye las pausas inter-grupo (§3.6): con 10 grupos y
# 300 s de pausa la ronda dura ~50 min; 2700 s mataba al hijo a mitad de ronda.
SHARE_TIMEOUT_MAX = int(os.environ.get("CXT_AUTO_SHARE_TIMEOUT_MAX", "7200"))
# Pausa entre grupos dentro de una ronda (§3.6): 300 s +-60 s por defecto.
AUTO_DELAY_S = int(float(os.environ.get("CXT_AUTO_DELAY_S", "300")))
AUTO_DELAY_JITTER_S = int(float(os.environ.get("CXT_AUTO_DELAY_JITTER_S", "60")))
# Estados de pareja (publicacion, grupo) que JAMAS se vuelven a seleccionar
# (§3.1). `ok` no esta aqui: se re-selecciona tras COOLDOWN_POST.
PAIR_TERMINAL = ("ok", "unknown", "pending", "blocked", "removed")
HALT_HOURS = float(os.environ.get("CXT_AUTO_HALT_HOURS", "72"))
# Cota del registro de intentos (crece con cada permalink visto). El TTL debe
# ser >= la caducidad de la cola (90 dias) para no re-compartir un item aun
# pendiente; los intentos "publicando" (en vuelo) NUNCA se podan.
ATTEMPTS_MAX = int(os.environ.get("CXT_AUTO_ATTEMPTS_MAX", "1000"))
ATTEMPTS_TTL_MS = int(float(os.environ.get("CXT_AUTO_ATTEMPTS_TTL_DAYS", "90")) * 24 * 3600 * 1000)

CHECKPOINT_PAT = ("/checkpoint", "/recover")

# Plan 5.6/9.7: `last_run.items` se capa para que el status no crezca sin
# control (un tope diario de 60 grupos deja margen de sobra).
LAST_RUN_ITEMS_CAP = 200
# Valores aceptados como "si" en las banderas de entorno (CXT_BACKGROUND...).
_TRUTHY = frozenset({"1", "true", "yes", "si", "sí", "on"})


def background_enabled(env=None):
    """CXT_BACKGROUND=1 (DEFAULT): la automatización usa pestaña propia y NUNCA
    activa/enfoca pestañas. Con 0 queda el camino viejo como rollback explícito
    (plan 9.7: feature flag con rollback inmediato)."""
    env = os.environ if env is None else env
    return str(env.get("CXT_BACKGROUND", "1")).strip().lower() in _TRUTHY


# Query params de tracking/consentimiento de FB: NO identifican el post.
_QS_TRACK = frozenset({
    "ref", "ref_src", "ref_url", "mibextid", "fbclid", "gclid",
    "__cft__", "__aaid", "__hs", "hc_location", "locale",
})


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------ logica pura

def clamp_interval(mins, lo=INTERVAL_MIN, hi=INTERVAL_MAX):
    try:
        m = float(mins)
    except (TypeError, ValueError):
        m = INTERVAL_REC
    return max(lo, min(hi, m))


def jitter_ms(base_ms, rng=None):
    r = (rng or random).uniform(-JITTER, JITTER)
    return int(base_ms * (1.0 + r))


def interval_ms(mins, rng=None, fast=False):
    """Retardo efectivo en ms con la banda dura respetada SIEMPRE (IS5).

    El jitter (+-30 %) se aplica PRIMERO, sobre el intervalo clampado; despues
    se recorta el resultado FINAL a [MIN*60000, MAX*60000]. Asi un valor en banda
    (p. ej. 15 -> ~[10.5, 19.5] min) conserva su jitter, pero un valor en el
    borde nunca sale de la banda: interval_ms(60) <= MAX y interval_ms(10) >= MIN.

    En FAST (modo rapido) el jitter es 0 y el suelo baja a 1 min; el tope MAX y
    el suelo duro del kick (KICK_MIN_MS) siguen intactos.
    """
    if fast:
        lo = int(fastmode.knobs()["min_interval_min"] * 60000)
        hi = int(INTERVAL_MAX * 60000)
        m = clamp_interval(mins, lo=fastmode.knobs()["min_interval_min"])
        return max(lo, min(hi, int(m * 60000)))
    lo = int(INTERVAL_MIN * 60000)
    hi = int(INTERVAL_MAX * 60000)
    return max(lo, min(hi, jitter_ms(clamp_interval(mins) * 60000, rng)))


def auto_delay_s(rng=None):
    """Pausa inter-grupo de la ronda: AUTO_DELAY_S con jitter +-60 s (§3.6)."""
    r = (rng or random).uniform(-AUTO_DELAY_JITTER_S, AUTO_DELAY_JITTER_S)
    return max(0, int(AUTO_DELAY_S + r))


def round_duration_ms(n, rng=None, fast=False):
    """Duracion estimada de una ronda de `n` grupos (para no solapar rondas).

    Una ronda de N grupos tarda ~N pausas inter-grupo; el `next_at` nunca puede
    quedar por debajo o dos rondas se pisarian sobre el mismo navegador. En FAST
    la pausa es la del perfil (1.5 s), no AUTO_DELAY_S.
    """
    if fast:
        pause = float(fastmode.knobs()["inter_group_delay_s"])
        return int(max(0, int(n)) * pause * 1000)
    return int(max(0, int(n)) * auto_delay_s(rng) * 1000)


def effective_interval(item=None, q=None):
    """Intervalo efectivo (min) para el item en curso, con prioridad (T23):

      0. `interval_override` de nivel superior de la cola (ACELERAR del popup):
         gana a TODO, para poder cambiar el ritmo de items YA encolados.
      1. el `interval_min` del PROPIO item en curso/siguiente
      2. el `interval_min` de nivel superior de la cola
      3. el recomendado INTERVAL_REC

    El modal envia `interval_min` como campo de nivel superior del POST
    /autopost; el daemon funde esos defaults DENTRO de cada item
    (server/main.go mergeAutopostDefaults), de modo que la eleccion del usuario
    vive en el item. Leer solo q["interval_min"] (clave que el daemon nunca
    escribe) hacia que el intervalo efectivo fuera siempre REC. Se clampa a
    [MIN, MAX] igual que antes.

    En FAST el intervalo es el minimo del perfil (1 min) y no se aplica jitter;
    un `interval_override` explicito sigue mandando, pero nunca baja del suelo
    FAST. El camino normal no cambia.
    """
    if fastmode.enabled(item=item):
        ov = q.get("interval_override") if isinstance(q, dict) else None
        if ov is not None:
            return clamp_interval(ov, lo=fastmode.knobs()["min_interval_min"])
        return float(fastmode.knobs()["min_interval_min"])
    ov = q.get("interval_override") if isinstance(q, dict) else None
    if ov is not None:
        return clamp_interval(ov)
    for src in (item, q):
        if isinstance(src, dict) and src.get("interval_min") is not None:
            return clamp_interval(src.get("interval_min"))
    return clamp_interval(INTERVAL_REC)


def first_next_at(now_ms):
    return int(now_ms) + int(INTERVAL_REC * 60000)


def next_at_for(now_ms, mins, rng=None, fast=False):
    return int(now_ms) + interval_ms(mins, rng, fast=fast)


def startup_next_at(now_ms, persisted_ms=0):
    """RE4: al arrancar, si el next_at persistido ya vencio se reprograma a
    ahora+REC (la primera publicacion del worker siempre espera >= REC, tambien
    tras un reinicio). Un next_at futuro se respeta (espera conservadora)."""
    now_ms = int(now_ms)
    persisted = int(persisted_ms or 0)
    if persisted > now_ms:
        return persisted
    return first_next_at(now_ms)


def roll_day(st, today):
    if st.get("day") != today:
        st["day"] = today
        st["done_today"] = 0
    return st


def under_daily_cap(done_today, cap=MAXDAY):
    return int(done_today or 0) < int(cap)


def group_limit(item_limit, cap=MAXGROUPS):
    n = int(item_limit or 0)
    if n <= 0:
        n = cap
    return min(n, cap)


# ------------------------------------------------------------ relevancia
# Defaults IDENTICOS a relevance.json: si el archivo falta o esta corrupto el
# worker no se queda sin criterio ni cae en "todo es relevante" (que seria la
# senal de spam mas caras). El jefe puede editar el JSON sin tocar codigo.
_REL_DEFAULT = {
    "pos_categories": ["compraventa", "guadalupan", "playa"],
    "pos_tokens": ["bazar", "venta", "ventas", "mercado", "vecinos",
                   "se entere", "enterarse", "emprendedor", "ofertas",
                   "remates", "comunidad", "noticias", "fraccionamiento"],
    "neg_tokens": ["bolsa de trabajo", "empleo", "vacante", "chamba",
                   "paypal", "free fire", "cripto", "prestamo", "préstamo",
                   "ganar dinero"],
}


def load_relevance(path=None):
    """Lee relevancia y completa claves faltantes con los defaults.

    Los tokens negativos GANAN siempre: un grupo de "bolsa de trabajo" no debe
    entrar aunque su categoria sea compraventa. Esa precedencia es la que evita
    publicar donde un admin va a reportar.
    """
    raw = cxtlib.load_json(path or RELEVANCE, {})
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for key, default in _REL_DEFAULT.items():
        value = raw.get(key)
        if isinstance(value, list):
            out[key] = [str(v) for v in value if str(v).strip()]
        else:
            out[key] = list(default)
    return out


def relevance_reason(group, rel=None):
    """Devuelve (relevante, motivo) para un grupo del ledger/groups.json.

    El motivo se registra en el log por cada descarte (F1-8): sin el, un grupo
    impertinente "desaparece" sin explicacion y parece un fallo del selector.
    `category` (groups.json) y `label` (columna del ledger) son el mismo dato.
    """
    rel = rel or load_relevance()
    group = group or {}
    name = cxtlib.norm(group.get("name") or "")
    label = cxtlib.norm(group.get("category") or group.get("label") or "")
    for token in rel["neg_tokens"]:
        needle = cxtlib.norm(token)
        if needle and needle in name:
            return False, "token_negativo:" + token
    cats = [cxtlib.norm(c) for c in rel["pos_categories"]]
    if label and label in cats:
        return True, "categoria:" + label
    for token in rel["pos_tokens"]:
        needle = cxtlib.norm(token)
        if needle and needle in name:
            return True, "token:" + token
    return False, "sin_relevancia"


def is_relevant(group):
    """Atajo booleano; la razon detallada vive en relevance_reason()."""
    return relevance_reason(group)[0]


def _utc_midnight(now):
    """Siguiente medianoche UTC en ms: cuando se libera el cupo diario."""
    now = int(now)
    day = ledger.utc_day(now)
    # timegm (no mktime): el dia es UTC y mktime lo interpretaria como local.
    base = int(calendar.timegm(time.strptime(day, "%Y-%m-%d"))) * 1000
    return base + 24 * 3600 * 1000


def _group_window(gid, group, post_map, post_key, now, rel, fast=False):
    """Evalua las restricciones DURAS de un grupo.

    Devuelve (elegible, motivo, libera_en_ms). `libera_en_ms` es el instante en
    que la restriccion deja de aplicar (para calcular `next_eligible_at`).
    El orden importa: relevancia, estado/cuarentena, warm, gap, cupo diario y,
    al final, enfriamiento de ESTA publicacion.

    En FAST se omiten el gap de grupo y el cooldown de la pareja (pacing
    anti-ban), pero se CONSERVAN los topes diarios, el estado/cuarentena, warm
    y el terminal de pareja: la seguridad no se toca.
    """
    group = group or {}
    ok_rel, why_rel = relevance_reason(group, rel)
    if not ok_rel:
        return False, why_rel, 0
    last_any = int(group.get("last_any") or 0)
    warm_at = int(group.get("warm_at") or 0)
    until = int(group.get("status_until") or 0)
    # Salto de reloj hacia atras: un `last_any` FUTURO (mas de 1 min) es un
    # evento pasado visto con reloj atrasado; se acota a ahora para que el gap
    # no bloquee 2 h de mas. `warm_at` y `status_until` SI pueden ser futuros
    # legitimamente (grupo nuevo en calentamiento / cuarentena), no se tocan.
    if last_any > now + 60000:
        last_any = now
    status = str(group.get("status") or "ok")
    day_n = int(group.get("day_n") or 0)
    if str(group.get("day_utc") or "") != ledger.utc_day(now):
        day_n = 0
    if status != "ok":
        if until > now:
            return False, "status:" + status, until
        return False, "status:" + status, now + 15 * 60000
    if until > now:
        return False, "status_until", until
    if warm_at > now:
        return False, "warm", warm_at
    if not fast and last_any and now - last_any < GAP_GRUPO:
        return False, "gap_grupo", last_any + GAP_GRUPO
    if day_n >= MAX_GRUPO_DIA:
        return False, "cupo_diario", _utc_midnight(now)
    row = (post_map or {}).get((str(gid), post_key)) or {}
    last_post = int(row.get("last_ts") or 0)
    if last_post > now + 60000:
        last_post = now
    pair_status = str(row.get("status") or "")
    # §3.1: una pareja que ya tuvo un resultado NO se repite salvo `ok` tras el
    # enfriamiento. `unknown` es terminal: sin prueba no se puede afirmar que no
    # se publico, y reintentar es justo la fuga que provoca bloqueos reales.
    if pair_status in PAIR_TERMINAL and pair_status != "ok":
        return False, "pair:" + pair_status, 0
    if not fast and last_post and now - last_post < COOLDOWN_POST:
        return False, "cooldown_post", last_post + COOLDOWN_POST
    return True, "", 0



def next_eligible_at(groups_map, post_map, post_key, now, rel=None,
                     exclude_gids=None, fast=False):
    """Instante en ms en que algun grupo NO EXCLUIDO puede volver a ser elegible.

    Si YA hay uno elegible devuelve `now`. Si no se puede calcular (todos
    irrelevantes), devuelve now+15 min: el item se reintenta despues en lugar
    de morir con `error: sin grupos`.

    `exclude_gids` (opcional) omite las parejas ya CUBIERTAS por cobertura
    (§3.5), las mismas que `select_round` descarta con `pair:covered`. Si tras
    excluirlas no queda NINGUN grupo no excluido, un `now` produciria el bucle
    de re-seleccion cada ~60 s (el item se difiere a `now`, `_tick` lo sube a
    `now+60 s` y se vuelve a elegir sin publicar): en ese caso se usa el suelo
    `now + INTERVAL_MIN*60000`. Si queda al menos un grupo no excluido, se
    devuelve su `free_at` real (recortado al mismo suelo, para no reintentar
    antes del intervalo minimo anti-ban).

    Compatibilidad: con `exclude_gids=None` (o una lista vacia) el calculo es
    IDENTICO al historico; solo cambia cuando hay exclusiones reales.
    """
    rel = rel or load_relevance()
    # El suelo se activa con la logica nueva (exclusiones explicitas). Con
    # `exclude_gids=None` NO se aplica para preservar el resultado historico.
    floor_ms = int(INTERVAL_MIN * 60000) if exclude_gids is not None else 0
    exclude = {str(g) for g in (exclude_gids or [])}
    best = None
    seen = False
    for gid, group in (groups_map or {}).items():
        if str(gid) in exclude:
            continue
        seen = True
        ok, _why, free_at = _group_window(gid, group, post_map, post_key, now, rel,
                                         fast=fast)
        if ok:
            return int(now)
        candidate = free_at if free_at > now else now + 15 * 60000
        if best is None or candidate < best:
            best = candidate
    if best is None:
        # Sin grupos no excluidos (todos cubiertos) se usa el suelo del
        # intervalo minimo; sin ningun grupo en el catalogo se conserva el
        # centinela historico de 15 min.
        return int(now) + (floor_ms if seen else 15 * 60000)
    return int(max(int(best), int(now) + floor_ms))


def coverage_state(groups_map, post_map, post_key, rel=None):
    """Cobertura de una publicacion sobre TODOS los grupos relevantes (§3.5).

    `exhausted` solo es True cuando cada grupo relevante ya tiene una fila
    TERMINAL para esta publicacion: ahi la publicacion alcanzo a todos y el
    item puede cerrarse. Un grupo relevante sin fila (o `inflight`) mantiene la
    cobertura abierta aunque la ronda ya haya salido bien.
    """
    rel = rel or load_relevance()
    relevant = []
    covered = []
    for gid in (groups_map or {}):
        group = (groups_map or {}).get(gid) or {}
        if not relevance_reason(group, rel)[0]:
            continue
        gid = str(gid)
        relevant.append(gid)
        row = (post_map or {}).get((gid, post_key)) or {}
        if str(row.get("status") or "") in PAIR_TERMINAL:
            covered.append(gid)
    return {"relevant": len(relevant), "covered_gids": covered,
            "covered": bool(relevant) and len(covered) == len(relevant),
            "exhausted": bool(relevant) and len(covered) == len(relevant)}


def select_round(groups_map, post_map, post_key, now=None, maxgroups=None,
                 daily_left=None, rng=None, rel=None, exclude_gids=None,
                 fast=False):
    """Selector de la ronda, en el orden ESTRICTO del contrato §4.1.

    1. relevancia (tokens negativos ganan)
    2. elegibilidad (estado, cuarentena, warm, gap de grupo, cupo diario)
    3. enfriamiento por publicacion (post_group.last_ts)
    4. orden por `last_ts` ascendente (nunca publicados primero) + azar
    5. N = min(MAXGROUPS, |elegibles|, cupo diario restante)

    Nunca devuelve error: `deferred=True` con `next_at` es el estado correcto
    cuando no hay elegibles, para que el item no muera ni se reintente en
    bucle. Las filas `inflight` cuentan como `last_ts` (intento reciente): no
    se re-publica a ciegas mientras una ronda anterior siga sin resolver.

    `exclude_gids` (opcional) saca de la ronda parejas ya CUBIERTAS para esta
    publicacion. Sin el (default `None`) se conserva el comportamiento
    historico: un `ok` vuelve a seleccionarse tras `COOLDOWN_POST` (F1-11).
    """
    now = int(now if now is not None else time.time() * 1000)
    rel = rel or load_relevance()
    rng = rng or random
    exclude = {str(g) for g in (exclude_gids or [])}
    discarded = []
    cands = []
    clock_jump = False
    for gid, group in (groups_map or {}).items():
        gid = str(gid)
        # Cobertura (§3.5): una pareja ya cubierta no se vuelve a publicar en
        # la misma campana. Va ANTES de `_group_window` para que el motivo sea
        # explicito y no se confunda con un cooldown.
        if gid in exclude:
            discarded.append({"gid": gid, "name": group.get("name") or "",
                              "reason": "pair:covered", "free_at": 0})
            continue
        ok, why, free_at = _group_window(gid, group, post_map, post_key, now, rel,
                                         fast=fast)
        # Solo un evento que DEBIO ocurrir en el pasado puede delatar un reloj
        # atrasado. warm_at/status_until futuros son estados legitimos.
        row_ts = int(((post_map or {}).get((gid, post_key)) or {}).get("last_ts") or 0)
        if int(group.get("last_any") or 0) > now + 60000 or row_ts > now + 60000:
            clock_jump = True
        if not ok:
            discarded.append({"gid": gid, "name": group.get("name") or "",
                              "reason": why, "free_at": int(free_at)})
            continue
        row = (post_map or {}).get((gid, post_key)) or {}
        cands.append({"gid": gid, "name": group.get("name") or "",
                      "last_ts": int(row.get("last_ts") or 0),
                      "tie": rng.random()})
    # Primero los nunca publicados (last_ts=0) y luego el mas antiguo; el azar
    # solo desempata para no repetir siempre el mismo orden del catalogo.
    cands.sort(key=lambda c: (c["last_ts"], c["tie"]))
    cap = int(MAXGROUPS if maxgroups is None else maxgroups)
    if daily_left is not None:
        cap = min(cap, max(0, int(daily_left)))
    n = max(0, min(cap, len(cands)))
    gids = [c["gid"] for c in cands[:n]]
    deferred = len(gids) == 0
    if deferred and cands:
        # Hay elegibles pero el cupo diario ya no deja publicar mas hoy.
        next_at = _utc_midnight(now)
    elif deferred:
        next_at = next_eligible_at(groups_map, post_map, post_key, now, rel,
                                   exclude_gids=exclude_gids, fast=fast)
    else:
        next_at = int(now)
    cov = coverage_state(groups_map, post_map, post_key, rel)
    return {"gids": gids, "eligible": len(cands), "deferred": deferred,
            "next_at": int(next_at), "discarded": discarded,
            "clock_jump": bool(clock_jump), "post_key": post_key,
            "relevant": cov["relevant"], "covered_gids": cov["covered_gids"],
            "covered": cov["covered"], "exhausted": cov["exhausted"]}


def intent_rows(gids, post_key, ts=None):
    """Filas de intencion para `ledger.intent` (todas con el mismo ts)."""
    ts = int(ts if ts is not None else time.time() * 1000)
    return [{"gid": str(gid), "post_key": post_key, "ts": ts} for gid in gids or []]


def is_dry(item):
    """P6: un item sin `dry` explicito es simulacion (fail-closed).

    Un unico helper evita que el gate de publicacion y lo que se reporta a la
    UI divergan: solo `dry: false` explicito publica de verdad.
    """
    return bool((item or {}).get("dry", True))


# Sufijos que la UI de Facebook pega al texto visible. Un `expect` heredado
# (encolado antes de T3.2) puede traerlos y entonces share_post ya no encuentra
# el post. Se recortan SOLO en memoria: la cola en disco no se toca.
_EXPECT_CUTS = ("... Ver más", "… Ver más", "Ver más", "Ver traducción",
                "Ver original", "hace aproximad...", "hace aproximad…",
                "hace aproximad")
_EXPECT_MAX = 160


def clean_expect(s):
    """Sanea el `expect` heredado en MEMORIA (nunca se persiste).

    Los items viejos guardaron el texto tal como lo pinta la UI ("… Ver más",
    "hace aproximad…", truncados). Se recorta el sufijo, se quitan los puntos
    suspensivos y se limita a 160 caracteres para que el match por texto vuelva
    a funcionar. Si al limpiar no queda nada se devuelve el original recortado:
    mejor un texto con ruido que perder la unica identificacion del item.
    """
    if not isinstance(s, str):
        return ""
    orig = s.strip()
    if not orig:
        return ""
    # Primer sufijo de UI: el que aparezca antes en el texto.
    cut = len(orig)
    for mark in _EXPECT_CUTS:
        i = orig.find(mark)
        if i != -1 and i < cut:
            cut = i
    out = orig[:cut].replace("…", " ").replace("...", " ")
    out = " ".join(out.split())
    if len(out) > _EXPECT_MAX:
        # Cortar en palabra si el ultimo espacio deja >= 80 chars utiles.
        space = out.rfind(" ", 0, _EXPECT_MAX + 1)
        out = out[:space] if space >= 80 else out[:_EXPECT_MAX]
        out = out.strip()
    return out or orig


def share_cmd_payload(item, gids, post_key):
    """Construye el payload del share conservando TODAS las claves historicas.

    `gids` manda en share_post (resuelve id->nombre y no hace match por nombre).
    `filter`/`limit` se conservan por compatibilidad con el CLI y el popup, pero
    el selector del worker es GLOBAL: el `filter` del item NO filtra la ronda;
    solo `gids` decide a donde se publica. `delay` aplica la pausa inter-grupo
    con jitter de §3.6, salvo en FAST (1.5 s fijos y `fast:true` para el hijo).
    `expect` sale saneado (red de seguridad para items viejos) y `expect_raw`
    conserva el texto original de la cola para diagnostico.
    """
    item = item or {}
    fast = bool(item.get("fast"))
    fast_pacing = fast or fastmode.enabled()
    raw_expect = item.get("expect_raw")
    if not raw_expect:
        raw_expect = item.get("expect")
    return {
        "url": item.get("url") or "",
        "expect": clean_expect(raw_expect),
        "expect_raw": raw_expect if raw_expect is not None else "",
        "text": item.get("text") or "",
        "dry": is_dry(item),
        "delay": (fastmode.knobs()["inter_group_delay_s"] if fast_pacing
                  else auto_delay_s()),
        "fast": fast,
        "composer": bool(item.get("composer")),
        "limit": group_limit(item.get("limit")),
        "filter": item.get("filter") or "",
        "gids": [str(g) for g in gids or []],
        "post_key": post_key or "",
    }


def parse_share_results(raw, gids=None):
    """Normaliza `results[]` del contrato viejo y del nuevo (gid/status/proof).

    El contrato nuevo manda: `status` decide si se acredita. Si el subproceso
    aun es el viejo (solo `ok`), se usa el orden de `gids` como identidad: el
    orden de salida de share_post es el de entrada, y asi el ledger no queda
    mudo durante el despliegue por partes.
    """
    results = (raw or {}).get("results") if isinstance(raw, dict) else None
    out = []
    for i, row in enumerate(results or []):
        if not isinstance(row, dict):
            continue
        gid = str(row.get("gid") or "")
        if not gid and gids and i < len(gids):
            gid = str(gids[i])
        status = str(row.get("status") or "")
        if not status:
            if row.get("pending"):
                status = "pending"
            elif row.get("ok"):
                status = "ok"
            else:
                status = "unknown"
        if row.get("pending"):
            status = "pending"
        out.append({"gid": gid, "status": status,
                    "ok": bool(row.get("ok")) and status == "ok",
                    "proof": str(row.get("proof") or ""),
                    "reason": str(row.get("reason") or "")})
    return out


def share_timeout(item, n=None):
    """Presupuesto TOTAL (s) del subproceso share_post para ESTA ronda.

    = GROUP_TIMEOUT * grupos + SHARE_MARGIN, recortado a
    [SHARE_TIMEOUT_MIN, SHARE_TIMEOUT_MAX]. Con selector, `n` debe ser
    len(gids): el item puede traer `limit` viejo y quedarse corto, matando la
    ronda a mitad y dejando filas inflight que no hacian falta."""
    if n is None:
        n = group_limit(item.get("limit")) if isinstance(item, dict) else MAXGROUPS
    n = max(1, int(n))
    # La pausa inter-grupo tambien consume reloj: sin sumarla el hijo moria por
    # timeout a mitad de una ronda perfectamente sana de 10 grupos.
    total = (GROUP_TIMEOUT + AUTO_DELAY_S) * n + SHARE_MARGIN
    return max(SHARE_TIMEOUT_FLOOR, SHARE_TIMEOUT_MIN,
               min(SHARE_TIMEOUT_MAX, total))


def permalink_key(url):
    """Clave de identidad del post, tolerante a tracking de Facebook.

    Antes se borraba TODO el querystring: `photo/?fbid=A` y `photo/?fbid=B`
    colapsaban a `…/photo`, así el dedupe tiraba items distintos y la cola
    quedaba vacía en la vista del worker. Ahora se conservan los parámetros
    de identidad (fbid, set, id, …) y solo se pegan los de tracking
    (`ref`, `__cft__…`, fbclid, etc.)."""
    u = (url or "").strip().split("#")[0]
    if "?" not in u:
        return cxtlib.norm(u.rstrip("/"))
    base, _, qs = u.partition("?")
    keep = []
    for part in qs.split("&"):
        if not part:
            continue
        name = part.split("=", 1)[0].lower()
        if name in _QS_TRACK or name.startswith("__cft") or name.startswith("mibextid"):
            continue
        keep.append(part)
    if not keep:
        return cxtlib.norm(base.rstrip("/"))
    return cxtlib.norm(base.rstrip("/") + "?" + "&".join(keep))


def target_key(item):
    """Sufijo de DESTINO del item: el conjunto de grupos que elige `filter`.

    Un mismo post puede salir UNA vez por conjunto distinto: haberlo compartido
    en 'guadalupana' no debe impedir compartirlo luego en 'playa del carmen'.
    Antes la identidad era solo el permalink, asi que re-publicar a otro grupo
    quedaba (a) deduplicado fuera de la cola y (b) marcado 'hecho' por el
    registro de intentos -> count_pending()=0 y el worker nunca arrancaba.
    `limit` NO entra en la clave: cambiar el cupo de grupos no es un destino
    nuevo (si entrara, subir el cupo re-publicaria en los grupos ya hechos)."""
    f = cxtlib.norm(item.get("filter") or "")
    return ("|f:" + f) if f else ""


def item_key(item, dry=None):
    """Clave estable del item para el registro de intentos.

    Permalink si lo hay; si no, el `expect`; y como ultimo recurso el texto.
    Se le anade el sufijo de destino (target_key) para que el a-lo-mas-una-vez
    sea por (post, conjunto de grupos). Un dry-run lleva sufijo ":dry": asi un
    fallo de simulacion se persiste (no se reintenta para siempre) pero NO
    contamina el a-lo-mas-una-vez del share real del mismo permalink."""
    if dry is None:
        dry = is_dry(item)
    key = permalink_key(item.get("url"))
    if not key:
        exp = cxtlib.norm(item.get("expect") or "")
        key = ("expect:" + exp) if exp else ("text:" + cxtlib.norm(item.get("text") or ""))
    return key + target_key(item) + (":dry" if dry else "")


def dedupe_items(items):
    """Colapsa duplicados por permalink + destino (mismo post, mismo conjunto de
    grupos). Dos items del mismo permalink con distinto `filter` son trabajos
    distintos y sobreviven; sin el sufijo, re-encolar a otro grupo se perdia."""
    seen = set()
    out = []
    for it in items:
        key = permalink_key(it.get("url"))
        if key:
            key += target_key(it)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(it)
    return out


def trim_depth(items, cap=DEPTH):
    return list(items)[:cap]


def queue_accepts(items, cap=DEPTH):
    return len(items) < cap


def is_expired(item, now_ms, ttl_ms=EXPIRE_MS):
    try:
        at = float(item.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return at > 0 and (int(now_ms) - at) > ttl_ms


def load_attempts():
    """Registro de intentos (propiedad del worker); nunca falla."""
    a = cxtlib.load_json(ATTEMPTS, {})
    return a if isinstance(a, dict) else {}


def prune_attempts(a, now_ms=None):
    """Acota el registro de intentos para que no crezca sin limite.

    Conserva SIEMPRE los estados "publicando" (intentos en vuelo) porque son los
    que garantizan la a-lo-mas-una-vez tras un reinicio; descarta lo mas viejo
    que el TTL y, si aun sobra, deja las N entradas mas recientes."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    keep = {}
    rest = []
    for k, v in a.items():
        if not isinstance(v, dict):
            continue
        if v.get("state") == "publicando":
            keep[k] = v
            continue
        try:
            at = int(v.get("at") or 0)
        except (TypeError, ValueError):
            at = 0
        if at and (int(now_ms) - at) > ATTEMPTS_TTL_MS:
            continue
        rest.append((k, v))
    if len(keep) + len(rest) > ATTEMPTS_MAX:
        rest.sort(key=lambda kv: int(kv[1].get("at") or 0), reverse=True)
        rest = rest[:max(0, ATTEMPTS_MAX - len(keep))]
    for k, v in rest:
        keep[k] = v
    return keep


def save_attempts(a):
    try:
        cxtlib.write_json_atomic(ATTEMPTS, prune_attempts(a))
    except Exception as e:
        log("save_attempts err " + str(e))


# Un fallo PURO (nada publicado) se puede reintentar hasta MAX_RETRIES intentos.
# Un intento con published>0 (exito parcial/total) NUNCA se reintenta: evita
# duplicar el post en los grupos donde ya salio.
MAX_RETRIES = 3


def attempt_mark(key, state, error="", published=None, pending=None,
                 unresolved=None, tries=None):
    """Marca a-lo-mas-una-vez en el archivo de intentos del worker.

    Se llama con "publicando" ANTES de invocar share_post y con "hecho"/"error"
    DESPUES. Nunca toca autoqueue.json (el daemon sigue siendo su dueño).

    Guarda ademas `tries` (n.o de intentos iniciados, se incrementa al marcar
    "publicando") y `published` (grupos que realmente publicaron, tomado de
    share_post_status.json) para que recover_states pueda decidir un reintento
    acotado sin arriesgar duplicados. `pending` marca que la ronda quedo
    pendiente de aprobacion del admin y se persiste para que _retryable se
    niegue a reintentarla (evita duplicar si el admin aprueba despues).

    `unresolved` (§3.2) es el numero de resultados sin prueba / gids sin
    resultado; persistirlo hace que `_retryable` no rearme nunca un `unknown`.
    `tries` explicito lo usa la caducidad para dejar el item terminal.
    """
    if not key:
        return
    a = load_attempts()
    prev = a.get(key) or {}
    n_tries = int(prev.get("tries", 0) or 0)
    if state == "publicando":
        n_tries += 1
    if tries is not None:
        n_tries = int(tries)
    pub = int(prev.get("published", 0) or 0)
    if published is not None:
        pub = int(published)
    pend = int(prev.get("pending", 0) or 0)
    if pending is not None:
        pend = 1 if pending else 0
    unres = int(prev.get("unresolved", 0) or 0)
    if unresolved is not None:
        unres = max(0, int(unresolved))
    a[key] = {"state": state, "error": (error or "")[:200],
              "at": int(time.time() * 1000),
              "tries": n_tries, "published": pub, "pending": pend,
              "unresolved": unres}
    save_attempts(a)


def _attempt_terminal(a):
    """True si el registro de intentos ya cerro (o esta cerrando) este item."""
    if not isinstance(a, dict):
        return False
    if str(a.get("state") or "") == "pendiente":
        # Ronda de cobertura a medias (§3.5): el item debe seguir eligiendose
        # para cubrir los grupos que faltan. `published>0` aqui NO es cierre;
        # un reintento CIEGO sigue bloqueado por `_retryable`.
        return False
    if str(a.get("state") or "") in ("hecho", "publicando"):
        return True
    return any(int(a.get(k, 0) or 0) > 0
               for k in ("published", "pending", "unresolved"))


def _retryable(it, a):
    """True si un item fallido puede reintentarse sin riesgo de duplicados.

    - Un dry NUNCA se reintenta solo (preserva el fix f7ab160 "dry retenido").
    - Si algo se publico (published>0) NUNCA se reintenta (evita duplicados).
    - Una ronda con grupos pendientes de aprobacion (pending>0) NUNCA se
      reintenta: si el admin la aprueba despues, el reintento duplicaria.
    - Un resultado sin prueba (`unresolved>0`, p. ej. `unknown`) NUNCA se
      reintenta: no se puede demostrar que no salio y republicar es la fuga
      que dispara el bloqueo de la cuenta (§3.1/§3.2).
    - Fallo puro sin publicar nada: reintenta hasta MAX_RETRIES intentos."""
    if is_dry(it):
        return False
    if int(a.get("published", 0) or 0) > 0:
        return False
    if int(a.get("unresolved", 0) or 0) > 0:
        return False
    # Una publicacion PENDIENTE de aprobacion no se reintenta: si el admin la
    # aprueba despues, un reintento publicaria lo mismo dos veces.
    if int(a.get("pending", 0) or 0) > 0:
        return False
    if int(a.get("tries", 0) or 0) >= MAX_RETRIES:
        return False
    return True


def _note_signal_quiet(kind, detail="", ts=None):
    """Registra una senal sin tumbar al llamador (la BD puede no existir aun)."""
    try:
        ledger.note_signal(kind, detail, ts=ts)
    except Exception as e:
        log("ledger senal %s err %s" % (kind, e))


def clear_attempt(url, force=False):
    """REINTENTAR manual (cmd del daemon): borra del registro de intentos las
    entradas de ERROR de ese permalink (clave normal y sufijo ":dry") para que
    recover_states lo rearme como pendiente. NUNCA borra un intento "hecho":
    reintentar algo ya publicado duplicaria el post. Devuelve True si cambio.

    P4: una ronda con `published>0` (ya publico en algun grupo) o
    `unresolved>0` (resultado sin prueba) NO se puede reintentar a ciegas: se
    rechaza sin borrar nada. Solo `force=True` (uso interno, no la UI) salta la
    guarda y deja constancia con `retry_forzado`.
    """
    key = permalink_key(url)
    if not key:
        return False
    a = load_attempts()
    # Las claves llevan sufijo de destino (|f:<filter>) y, en dry, ":dry". Se
    # consideran TODAS las de ese permalink (cualquier destino).
    keys = [k for k in a.keys()
            if k == key or k.startswith(key + "|f:") or k.startswith(key + ":dry")]
    reason = ""
    for k in keys:
        e = a.get(k)
        if not isinstance(e, dict):
            continue
        if int(e.get("published", 0) or 0) > 0:
            reason = "ya_publicado"
            break
        if int(e.get("unresolved", 0) or 0) > 0:
            reason = "parcial_sin_confirmar"
    if reason:
        if not force:
            _note_signal_quiet("retry_rechazado", reason)
            return False
        _note_signal_quiet("retry_forzado", reason)
    changed = False
    for k in keys:
        e = a.get(k)
        if isinstance(e, dict) and e.get("state") == "error":
            a.pop(k, None)
            changed = True
    if changed:
        save_attempts(a)
    return changed


def recover_states(items, attempts=None):
    """Reconciliacion al leer la cola (sin escribirla).

    - Un estado persistido "publicando" ya se intento -> "hecho".
    - Si el registro de intentos tiene la clave, ese item ya se intento:
      "publicando"/"hecho" -> hecho, "error" -> error. Nunca se re-comparte.
    - Un dry usa clave con sufijo ":dry" (ver item_key): su fallo persiste
      como "error" en la cola en memoria sin bloquear el share real del
      mismo permalink (clave sin sufijo, distinta)."""
    if attempts is None:
        attempts = load_attempts()
    for it in items:
        if (it.get("state") or "pendiente") == "publicando":
            it["state"] = "hecho"
            it["error"] = it.get("error") or "recuperado tras reinicio"
        if not attempts:
            continue
        a = attempts.get(item_key(it))
        if not a and is_dry(it):
            a = attempts.get(item_key(it, dry=True))
        if not a:
            continue
        stt = a.get("state")
        # El estado RESUELTO se expone al popup: n.o de grupos que publicaron y
        # n.o de intentos (para mostrar "hecho 6/6" o "error 0/6") sin obligar a
        # la UI a leer el registro de intentos.
        it["published"] = int(a.get("published", 0) or 0)
        it["tries"] = int(a.get("tries", 0) or 0)
        if stt == "publicando":
            it["state"] = "hecho"
            it["error"] = it.get("error") or "intento interrumpido (at-most-once)"
        elif stt == "hecho":
            it["state"] = "hecho"
        elif stt == "error":
            if _retryable(it, a):
                it["state"] = "pendiente"
                it["error"] = ""
            else:
                it["state"] = "error"
                # §3.2: el motivo NUNCA es mudo. Si hubo resultados sin prueba
                # se dice explicitamente por que no se reintenta.
                unres = int(a.get("unresolved", 0) or 0)
                if unres > 0:
                    it["error"] = "%d sin prueba (no se reintenta)" % unres
                else:
                    it["error"] = it.get("error") or a.get("error") or "error"
    return items


def is_checkpoint_url(url):
    u = (url or "").lower()
    return any(p in u for p in CHECKPOINT_PAT)


ALERT_COOLDOWN_MS = 10 * 60 * 1000


def note_alert(st, reason, now=None):
    """Deja un aviso visible sin pausar la cola: solo alerta, nunca restringe."""
    now = int(now if now is not None else time.time() * 1000)
    reason = str(reason or "aviso")
    prev = st.get("alerta") if isinstance(st.get("alerta"), dict) else {}
    if (prev.get("reason") == reason
            and now - int(prev.get("at") or 0) < ALERT_COOLDOWN_MS):
        return st
    st["alerta"] = {"reason": reason, "at": now}
    st["error"] = "ALERTA: " + reason
    log("ALERTA: " + reason)
    return st


def pause_for_checkpoint(st):
    # Se conserva el nombre por compatibilidad, pero ya NO pausa: solo alerta.
    return note_alert(st, "cuenta restringida")


def apply_checkpoint(st, url):
    if is_checkpoint_url(url):
        pause_for_checkpoint(st)
        return True
    return False


def set_waiting(st, job):
    """Otro job usa la pestaña: el estado visible marca esperando:<job>."""
    st["current"] = "esperando:" + str(job)
    st["error"] = ""
    return st


def clear_waiting(st):
    if str(st.get("current") or "").startswith("esperando:"):
        st["current"] = None
    return st


def bad_item(item):
    # Un item sin url NI expect no puede identificar una publicacion: compartir
    # con url="" haria que share_post compartiera el post visible (el equivocado).
    if not (item.get("url") or item.get("expect")):
        return "item sin url ni expect"
    return ""


def item_outcome(share_st):
    if not isinstance(share_st, dict) or not share_st:
        return "error", "sin estado de share_post"
    if share_st.get("partial"):
        # Una ronda cortada por timeout NO es "hecho" aunque los grupos que
        # alcanzaron a salir esten ok: faltan gids. published>0 evita el
        # reintento ciego; los ausentes quedan inflight para reconciliar.
        err = share_st.get("error") or "ronda parcial (timeout)"
        return "error", str(err)[:200]
    err = share_st.get("error") or ""
    if err:
        return "error", str(err)[:200]
    results = share_st.get("results") or []
    if not results:
        return "error", "share_post sin resultados"
    # `status` (contrato nuevo) manda sobre `ok`: un gid distinto o sin prueba
    # llega como unknown y NUNCA puede contar como exito.
    failed = [r for r in results
              if not (r.get("ok") and str(r.get("status") or "ok") == "ok")]
    pending = [r for r in results
               if r.get("pending") or str(r.get("status") or "") == "pending"]
    if pending:
        return "error", "pendiente-aprobacion en %d grupo(s)" % len(pending)
    if failed:
        return "error", "fallo en %d grupo(s)" % len(failed)
    return "hecho", ""


def count_pending(items):
    # Un item `diferido` sigue siendo trabajo pendiente (solo espera a que haya
    # grupos elegibles). Excluirlo pondria pending=0 y el popup creeria que la
    # cola termino.
    return len([it for it in items
                if (it.get("state") or "pendiente") in ("pendiente", "diferido")])


def pick_pending(items, now_ms):
    for it in items:
        if (it.get("state") or "pendiente") != "pendiente":
            continue
        if is_expired(it, now_ms):
            it["state"] = "error"
            it["error"] = "expirado"
            # §3.8: la caducidad se persiste; si no, recover_states la rearmaba
            # en cada tick (tries=0) y el item expirado volvia a publicarse.
            attempt_mark(item_key(it, dry=is_dry(it)), "error",
                         "expirado", tries=MAX_RETRIES)
            continue
        return it
    return None


# ------------------------------------------------------------ IO atomica

def read_queue():
    """Lee la cola (SOLO lectura; el daemon es su unico escritor) y aplica la
    reconciliacion a-lo-mas-una-vez en memoria."""
    q = cxtlib.load_json(QUEUE, {})
    if not isinstance(q, dict):
        q = {}
    q.setdefault("paused", False)
    raw = q.get("items")
    if not isinstance(raw, list):
        raw = []
    q["items"] = trim_depth(dedupe_items([it for it in raw if isinstance(it, dict)]))
    recover_states(q["items"])
    return q


def _queue_sig(q):
    """Firma del CONTENIDO de la cola que dispara un re-evaluar YA: el conjunto
    de items (enqueue/del/clear) y el override de intervalo (ACELERAR). NO mira
    `paused`: pausar/reanudar no debe adelantar la publicacion ni saltarse el
    intervalo (la pausa se sincroniza aparte, cada tick). Usa las MISMAS claves
    que el a-lo-mas-una-vez (permalink + destino), de modo que reescribir la
    cola sin cambios reales no dispara un falso kick."""
    q = q or {}
    items = q.get("items") or []
    keys = tuple(item_key(it) for it in items if isinstance(it, dict))
    return (keys, q.get("interval_override"))


def default_status():
    return {"running": False, "pausado": False, "pending": 0, "current": None,
            "next_at": 0, "done": 0, "results": [], "items": [], "error": "",
            "started_ms": 0, "finished_ms": 0,
            "dom": {"tagged": 0, "injected": 0, "errors": 0},
            "day": "", "done_today": 0, "deferred": 0, "alerta": {},
            # Pestaña propia (plan 5.2.2) y perfil fijado (plan 5.5.4/5.6).
            "auto_tab": None, "profile_dir": "", "profile_name": "",
            "last_run": {}}


def load_status():
    st = cxtlib.load_json(STATUS, {})
    if not isinstance(st, dict):
        st = {}
    for k, v in default_status().items():
        st.setdefault(k, v)
    if not isinstance(st.get("dom"), dict):
        st["dom"] = {"tagged": 0, "injected": 0, "errors": 0}
    else:
        for k, v in default_status()["dom"].items():
            st["dom"].setdefault(k, v)
    if not isinstance(st.get("results"), list):
        st["results"] = []
    if not isinstance(st.get("items"), list):
        st["items"] = []
    return st


def write_status(st):
    # La alerta es un aviso persistente: si un tick limpia `error`, debe seguir visible.
    alerta = st.get("alerta")
    if isinstance(alerta, dict) and alerta.get("reason") and not st.get("error"):
        st["error"] = "ALERTA: " + str(alerta["reason"])
    cxtlib.write_status(STATUS, st)


def consume_cmd(path):
    if not os.path.exists(path):
        return {}
    cmd = cxtlib.load_json(path, {})
    try:
        os.remove(path)
    except OSError:
        pass
    return cmd if isinstance(cmd, dict) else {}


def _token():
    tok = os.environ.get("CXT_TOKEN")
    if tok:
        return tok.strip()
    try:
        with open(os.path.join(DIR, "token"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def health():

    url = "http://127.0.0.1:%s/health" % PORT
    tok = _token()
    req = urllib.request.Request(url)
    if tok:
        req.add_header("X-CXT-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        log("health err %s" % e)
        return None


def _as_tab_id(tab):
    """Normaliza el retorno de cxtlib: id entero o dict {tabId, windowId}."""
    if isinstance(tab, dict):
        tab = tab.get("tabId", tab.get("id"))
    try:
        return int(tab)
    except (TypeError, ValueError):
        return None


# Id de la pestaña propia de ESTE proceso. Se resuelve una sola vez para no
# crear una pestaña por ronda; `reset_auto_tab()` permite diagnóstico/tests.
_AUTO_TAB = {"id": None, "err_at": 0}


def reset_auto_tab():
    """Olvida la pestaña propia cacheada (diagnóstico y tests)."""
    _AUTO_TAB["id"] = None
    _AUTO_TAB["err_at"] = 0


def _auto_tab_log(msg):
    """Log con anti-spam: el tick pesado reintenta a ~1 Hz."""
    now = int(time.time())
    if now - int(_AUTO_TAB.get("err_at") or 0) < 30:
        return
    _AUTO_TAB["err_at"] = now
    log(msg)


def _dedicated_tab():
    """Pestaña PROPIA de la automatización, sin activarla ni enfocar la ventana.

    CXT_BACKGROUND=1 (default): delega en `cxtlib.ensure_background_tab`, que
    llama a la op `tab.ensure_auto`. Se llama en cada ronda a propósito: la
    extensión valida el id guardado y crea otra pestaña si el usuario la cerró.
    Así nunca se ejecuta CDP contra un id muerto. El id se publica en
    `st["auto_tab"]` (status/popup). Un `CXT_TAB` numérico explícito manda.

    CXT_BACKGROUND=0: rollback al descubrimiento viejo (`resolve_tab`). Aquí
    tampoco se activa nada: el foco lo robaba share_post, no este worker.
    En ningún camino se llama `tab.activate`.
    """
    if not background_enabled():
        try:
            return _as_tab_id(cxtlib.resolve_tab(CXT))
        except Exception as e:
            log("resolve_tab err " + str(e))
            return None
    env_tab = _as_tab_id(os.environ.get("CXT_TAB"))
    if env_tab is not None:
        log("pestana propia fijada por CXT_TAB=%d" % env_tab)
        _AUTO_TAB["id"] = env_tab
        _AUTO_TAB["err_at"] = 0
        return env_tab
    ensure = getattr(cxtlib, "ensure_background_tab", None)
    try:
        if ensure is not None:
            tab = _as_tab_id(ensure(CXT))
        else:
            # Despliegue por partes: cxtlib viejo. Se resuelve sin activar
            # para no robar foco mientras llega la API nueva.
            _auto_tab_log("cxtlib sin ensure_background_tab: "
                          "resolve_tab sin activar")
            tab = _as_tab_id(cxtlib.resolve_tab(CXT))
    except Exception as e:
        _auto_tab_log("ensure_background_tab err " + str(e))
        return None
    if tab is None:
        # Navegador/extension no disponibles: se reintenta, sin spam.
        _auto_tab_log("pestana propia no disponible (sin navegador?)")
        return None
    _AUTO_TAB["id"] = tab
    _AUTO_TAB["err_at"] = 0
    return tab


def _looks_like_path(value):
    """True si el perfil parece RUTA (absoluta/relativa con separador).

    Un nombre suelto ("Default", "Profile 1") es una etiqueta del perfil de
    Chrome, no una ruta: no se puede comprobar con el sistema de archivos.
    """
    v = str(value or "")
    return v.startswith(("/", "~", ".")) or os.sep in v


# Perfil FIJADO de este job (plan 5.5.4/9.7). Vive en memoria del worker: se
# resuelve una vez y no cambia aunque el entorno o el status se reescriban.
_PROFILE_PIN = {}


def reset_profile_pin():
    """Olvida el perfil fijado (diagnóstico y tests)."""
    _PROFILE_PIN.clear()


def profile_pin(st=None, env=None):
    """Fija y devuelve {profile_dir, profile_name, available} para este job.

    Precedencia: CXT_PROFILE_DIR/CXT_PROFILE_NAME, luego
    `st["profile_dir"]/["profile_name"]`. Sin datos el perfil queda vacío (no
    se inventa una etiqueta). Si CXT_PROFILE_DIR parece ruta y no existe, se
    emite una ALERTA (sin pausar) y `available` queda False: NUNCA se sustituye
    por otro perfil en silencio.
    """
    if _PROFILE_PIN:
        pin = dict(_PROFILE_PIN)
        if not pin.get("available") and isinstance(st, dict) and st:
            # El pin pudo fijarse antes de tener status (run_share): la alerta
            # se emite en cuanto hay donde mostrarla. `note_alert` la deduplica.
            note_alert(st, "perfil no disponible: " + pin["profile_dir"])
        return pin
    env = os.environ if env is None else env
    st = st if isinstance(st, dict) else {}
    pdir = str(env.get("CXT_PROFILE_DIR") or st.get("profile_dir") or "").strip()
    pname = str(env.get("CXT_PROFILE_NAME") or st.get("profile_name") or "").strip()
    available = True
    if pdir and _looks_like_path(pdir):
        available = os.path.isdir(os.path.expanduser(pdir))
    _PROFILE_PIN.update({"profile_dir": pdir, "profile_name": pname,
                         "available": available})
    if not available and st:
        note_alert(st, "perfil no disponible: " + pdir)
    return dict(_PROFILE_PIN)


def _tab_list():
    try:
        r = cxtlib.call(CXT, "tabs", {})
    except Exception as e:
        log("tab_list err %s" % e)
        r = None
    tabs = r if isinstance(r, list) else (r or {}).get("tabs") or []
    return tabs if isinstance(tabs, list) else []


def _tab_urls():
    return [t.get("url") for t in _tab_list()
            if isinstance(t, dict) and t.get("url")]


def active_tabs():
    """Ids de las pestañas que el USUARIO tiene activas ahora mismo (una por
    ventana). SOLO se usa con CXT_BACKGROUND=0: con la pestaña propia no hay
    foco que guardar ni restaurar (plan 5.2.2)."""
    return [t.get("id") for t in _tab_list()
            if isinstance(t, dict) and t.get("active") and t.get("id") is not None]


def restore_tabs(ids):
    """Rollback de CXT_BACKGROUND=0: reactiva las pestañas del usuario.

    Con CXT_BACKGROUND=1 NO se llama: `tab.activate` enfoca la ventana y es
    justo el salto que este cambio elimina. Se conserva solo como diagnóstico
    del camino viejo."""
    for tid in ids or []:
        try:
            cxtlib.call(CXT, "tab.activate", {"tabId": tid})
        except Exception as e:
            log("restore_tabs err %s" % e)


JS_WARN = ("(()=>{for(const x of document.querySelectorAll('[role=dialog]')){"
           "const t=(x.innerText||'').toLowerCase();"
           "if(/checkpoint|restring|suspend|bloquead|inhabilit|"
           "temporarily blocked|blocked|temporarily restricted|restricted/i"
           ".test(t))return true;}"
           "return false;})()")


def warning_dialog(tab):
    if tab is None:
        return False
    try:
        return bool(cxtlib.cdp(CXT, tab, JS_WARN))
    except Exception as e:
        log("warning_dialog err %s" % e)
        return False


JS_BLOCK = ("(()=>{for(const x of document.querySelectorAll('[role=dialog]')){"
            "const t=(x.innerText||'').toLowerCase();"
            "if(/bloquead|temporarily blocked|blocked|temporarily restricted|"
            "restricted/i.test(t))return true;}"
            "return false;})()")


def block_notice(tab):
    """True si el dialogo avisa de un bloqueo temporal (parada dura §3.7)."""
    if tab is None:
        return False
    try:
        return bool(cxtlib.cdp(CXT, tab, JS_BLOCK))
    except Exception as e:
        log("block_notice err %s" % e)
        return False


def checkpoint_now(tab):
    # Devuelve la url del tab que dispara el checkpoint ('' si no hay).
    for u in _tab_urls():
        if is_checkpoint_url(u):
            return u
    # P5: el aviso de BLOQUEO se evalua SIEMPRE, no solo si `warning_dialog`
    # matcheo. Un "temporarily blocked" en ingles debe alcanzar la parada dura.
    if block_notice(tab):
        return "block"
    if warning_dialog(tab):
        return "dialog"
    return ""


def _share_status_since(t0):
    """Lee share_post_status.json SOLO si lo escribio esta ronda.

    Sin la comprobacion de mtime un status viejo (ok:true) se contaria como
    exito de una ronda que murio antes de escribir nada.
    """
    try:
        mtime = os.path.getmtime(SHARE_STATUS)
    except OSError:
        return None
    if mtime >= t0 - 2:
        share_st = cxtlib.load_json(SHARE_STATUS, {})
        return share_st if isinstance(share_st, dict) and share_st else None
    # §3.8: un salto de reloj hacia atras deja el mtime "viejo" aunque el
    # status SI sea de esta ronda. Se acepta por `finished_at` +-600 s.
    share_st = cxtlib.load_json(SHARE_STATUS, {})
    if not isinstance(share_st, dict) or not share_st:
        return None
    finished = int(share_st.get("finished_at") or 0)
    if finished and abs(finished - int(t0 * 1000)) <= 600000:
        return share_st
    return None


# Costura de test: los fixtures parchean `subprocess.run` para simular un
# timeout sin lanzar share_post. En produccion SIEMPRE se usa Popen, que es el
# unico que expone el hijo para matarlo en `_on_term` (§3.4).
_ORIG_RUN = subprocess.run
_CHILD = None


def share_cmd_path(batch=""):
    """Path UNICO del cmd de esta ronda: jamas el `share_post.cmd.json` del
    daemon (§3.4). El nombre sale del batch o de pid+reloj como respaldo."""
    token = "".join(ch for ch in str(batch or "") if ch.isalnum() or ch in "-_")
    if not token:
        token = "r%d-%d" % (os.getpid(), int(time.time() * 1000))
    return os.path.join(DIR, "share_post.cmd.%s.json" % token)


def _kill_child(proc=None):
    """Mata el hijo y su grupo de procesos (best-effort, nunca propaga)."""
    proc = proc if proc is not None else _CHILD
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass


def share_log_path(batch=""):
    """Log de la ronda: mismo token que el cmd, bajo DIR/logs.

    Antes el hijo iba con stdout/stderr a DEVNULL y una ronda fallida dejaba
    el log del worker mudo (solo se veia el status final). El hijo pasa por
    su propio log para poder reconstruir que hizo en cada grupo.
    """
    token = "".join(ch for ch in str(batch or "") if ch.isalnum() or ch in "-_")
    if not token:
        token = "r%d-%d" % (os.getpid(), int(time.time() * 1000))
    return os.path.join(DIR, "logs", "share-%s.log" % token)


def _prune_share_logs(keep=20):
    """Conserva los `keep` logs de ronda mas recientes (best-effort)."""
    folder = os.path.join(DIR, "logs")
    try:
        names = [n for n in os.listdir(folder) if n.startswith("share-")
                 and n.endswith(".log")]
        names.sort(key=lambda n: os.path.getmtime(os.path.join(folder, n)))
        for name in names[:-keep]:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    except OSError:
        pass


def _run_share_child(cmd, budget, env, log_path=None):
    """Lanza share_post y deja su Popen en `_CHILD` mientras corre."""
    global _CHILD
    if subprocess.run is not _ORIG_RUN:
        subprocess.run(cmd, capture_output=True, text=True,
                       timeout=budget, env=env)
        return
    proc = None
    sink = subprocess.DEVNULL
    log_f = None
    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_f = open(log_path, "ab")
            sink = log_f
        except OSError as e:
            log("share log err %s" % e)
            log_f = None
            sink = subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=sink,
                            stderr=subprocess.STDOUT if log_f else subprocess.DEVNULL,
                            env=env, start_new_session=True)
    _CHILD = proc
    try:
        proc.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        _kill_child(proc)
        raise
    finally:
        _CHILD = None
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass


def run_share(item):
    """Ejecuta share_post para los gids ya elegidos por el selector.

    El item lleva `_gids`/`_post_key`/`_lock_held`/`_batch` como campos
    TRANSITORIOS (nunca se persisten en autoqueue.json). Al timeout se devuelve
    el status PARCIAL: contar published=0 cuando algo salio es la causa directa
    de los duplicados por reintento.

    El hijo recibe SIEMPRE un `--cmd` propio de la ronda (§3.4); ese archivo se
    borra en el `finally` aunque el subproceso muera o agote el tiempo.

    Foco (plan 5.2.2): el hijo recibe `CXT_TAB` de la pestaña PROPIA del
    worker y `CXT_BACKGROUND=1` para que tampoco active nada. Con
    CXT_BACKGROUND=0 se conserva el guardar/restaurar pestañas viejo.
    Perfil (plan 5.5.4): el job queda fijado; el hijo hereda el mismo perfil.
    """
    n = group_limit(item.get("limit"))
    gids = [str(g) for g in (item.get("_gids") or [])]
    budget = share_timeout(item, len(gids) or None)
    post_key = str(item.get("_post_key") or "")
    cfg = share_cmd_payload(item, gids, post_key)
    cmd_path = share_cmd_path(item.get("_batch") or "")
    cxtlib.write_json_atomic(cmd_path, cfg)
    env = dict(os.environ)
    if item.get("_lock_held"):
        # share_post NO debe volver a tomar el flock: lo tiene este worker
        # durante toda la ronda. Sin esta marca el hijo saldria "browser
        # ocupado por otro driver" contra su propio padre.
        env["CXT_LOCK_HELD"] = "1"
    if fastmode.enabled(item=item):
        # FAST por item o por entorno: el hijo (y shared.py) lo leen al vuelo.
        env["CXT_FAST"] = "1"
    # Sin `st`: el perfil se fija con el entorno; el status (alerta + campos)
    # se actualiza en `_publish`/`_tick`, que si tienen el estado del worker.
    pin = profile_pin()
    env["CXT_PROFILE_DIR"] = pin["profile_dir"]
    env["CXT_PROFILE_NAME"] = pin["profile_name"]
    if not pin["available"] and pin["profile_dir"]:
        # No se publica en un perfil que no es el fijado: alerta y ronda fallida
        # explicita (nunca un cambio silencioso de perfil, plan 5.5.4).
        msg = "perfil no disponible: " + pin["profile_dir"]
        try:
            os.remove(cmd_path)
        except OSError as e:
            log("cmd cleanup err " + str(e))
        return {"ok": False, "error": msg, "results": [], "published": 0,
                "pending": 0}
    bg = background_enabled()
    if bg:
        env["CXT_BACKGROUND"] = "1"
    tab = _dedicated_tab()
    if tab is not None:
        env["CXT_TAB"] = str(tab)

    # Estatus rancio: borrar el status anterior ANTES de correr share_post, si
    # no un share que muere dejaria el ok:true de la corrida previa y lo
    # contariamos como "hecho".
    try:
        os.remove(SHARE_STATUS)
    except OSError:
        pass
    t0 = time.time()
    log("share %d grupo(s) -> tope %ss (%ss/grupo)%s"
        % (len(gids) or n, budget, GROUP_TIMEOUT,
           (" tab propia=%s" % tab) if tab is not None else ""))

    # Rollback (CXT_BACKGROUND=0): share_post activa su pestaña y movía el foco
    # del usuario; se guardan las activas y se restauran pase lo que pase.
    # Con CXT_BACKGROUND=1 no hay nada que guardar: el hijo no activa.
    prev_active = None if bg else active_tabs()
    share_log = share_log_path(item.get("_batch") or "")
    log("share log: %s" % share_log)
    try:
        _run_share_child(["python3", "-u", SHARE, "--cmd", cmd_path],
                         budget, env, log_path=share_log)
    except subprocess.TimeoutExpired:
        share_st = _share_status_since(t0)
        msg = ("timeout tras %ss (presupuesto %ss/grupo x %d grupos)"
               % (budget, GROUP_TIMEOUT, len(gids) or n))
        if share_st is not None:
            share_st["ok"] = False
            share_st["partial"] = True
            share_st["timeout"] = True
            share_st["error"] = msg
            return share_st
        return {"ok": False, "partial": True, "timeout": True,
                "results": [], "published": 0, "pending": 0, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        if prev_active is not None:
            restore_tabs(prev_active)
        try:
            os.remove(cmd_path)
        except OSError as e:
            log("cmd cleanup err " + str(e))
        _prune_share_logs()

    share_st = _share_status_since(t0)
    if share_st is None:
        return {"ok": False, "error": "share_post no dejo estado"}
    return share_st


# ------------------------------------------------------------ ledger
def apply_round_results(gids, post_key, raw, batch="", ts=None, db=None, dry=False):
    """Traduce la evidencia de share_post a filas del ledger, POR GRUPO.

    - `ok` solo si el gid coincide y hay permalink/prueba (lo decide el status).
    - `unknown` cuando no hay evidencia: NO se reintenta a ciegas.
    - Ronda parcial (timeout): los gids sin resultado quedan `inflight` para la
      reconciliacion del siguiente arranque; no se inventa un resultado.
    - Una ronda TERMINADA sin `results` no deja `inflight`: sin evidencia se
      sella `unknown` (terminal).
    Los contadores `day_n`/`last_any` los lleva `ledger.intent` al abrir la
    ronda (P2); aqui no se vuelven a incrementar.
    Un dry-run no escribe el ledger: no acredita ninguna publicacion real.
    """
    gids = [str(g) for g in gids or []]
    summary = parse_share_results(raw, gids)
    published = len([r for r in summary if r["ok"]])
    pending = len([r for r in summary if r["status"] == "pending"])
    out = {"results": summary, "published": published, "pending": pending,
           "unresolved": []}
    if dry or not gids:
        return out
    ts = int(ts if ts is not None else time.time() * 1000)
    partial = bool((raw or {}).get("partial"))
    seen = set()
    for entry in summary:
        gid = entry["gid"]
        if not gid or gid not in gids:
            continue
        seen.add(gid)
        try:
            ledger.result(gid, post_key, entry["status"], entry["ok"],
                          entry["proof"], batch, ts, db)
            # F1-13/P2: garantiza `day_n>=1` sin doble contar lo que ya conto
            # `intent` (mismo dia nunca incrementa). El contador sigue siendo
            # del intento; esto solo cubre filas sin intento previo.
            ledger.ensure_group_attempt(gid, ts, db=db)
            # P2: aqui NO se incrementan `day_n`/`last_any`; hacerlo seria
            # doble conteo del mismo intento. Solo se mantiene la cuarentena de
            # los grupos que bloquearon o borraron.
            if entry["status"] in ("blocked", "removed"):
                # Cuarentena corta: un grupo que bloqueo/borro no debe recibir
                # otra ronda manana a la misma hora.
                ledger.group_state(gid, db=db, status="blocked",
                                   status_until=ts + 24 * 3600 * 1000)
        except Exception as e:
            log("ledger result err gid=%s %s" % (gid, e))
    unseen = [g for g in gids if g not in seen]
    if not partial:
        # La ronda TERMINO y el grupo no reporto nada: sin evidencia => unknown.
        # Cubre tambien la ronda sin `results` (share_post no dejo estado).
        for gid in unseen:
            try:
                ledger.result(gid, post_key, "unknown", 0, "", batch, ts, db)
            except Exception as e:
                log("ledger unknown err gid=%s %s" % (gid, e))
    # P3: una ronda PARCIAL sella como `unknown` terminal los gids que no
    # dieron resultado. Los que SI reportaron (incluidos ok) no se tocan.
    if partial and unseen:
        try:
            ledger.seal_partial(batch, gids=unseen, now=ts, db=db)
        except Exception as e:
            log("ledger seal_partial err " + str(e))
    out["unresolved"] = unseen
    return out


def round_outcome(raw, gids, applied):
    """Veredicto del ITEM a partir de los gids realmente pedidos.

    `item_outcome(raw)` a secas se dejaria convencer por un `ok` de un gid que
    no era el objetivo (regla dura §3: gid distinto => unknown, nunca ok). Con
    gids, el exito exige que TODOS los pedidos tengan status ok; cualquier
    ausencia (parcial/timeout) es error y `published` evita el reintento ciego.
    """
    gids = [str(g) for g in gids or []]
    if not gids:
        return item_outcome(raw)
    if (raw or {}).get("partial"):
        return "error", str((raw or {}).get("error")
                            or "ronda parcial (timeout)")[:200]
    rows = (applied or {}).get("results") or []
    if not rows:
        return item_outcome(raw)
    ok_gids = {r["gid"] for r in rows if r.get("ok")}
    missing = [g for g in gids if g not in ok_gids]
    if missing:
        return "error", "fallo en %d grupo(s)" % len(missing)
    return "hecho", ""


def _default_evidence(row, db=None):
    """Evidencia afirmativa para un `inflight` usando shared.py (sin editarlo).

    `confirm_new_post_permalink` es la unica ruta que puede probar un post
    NUEVO por permalink (el feed de varios grupos no lista lo propio). Si no
    hay pestaña, extension apagada o `shared` falla, devuelve None: el llamador
    degrada a `unknown` y NO re-publica.
    """
    try:
        import shared
    except Exception as e:
        log("reconcile: shared no disponible (%s)" % e)
        return None
    tab = _dedicated_tab()
    if tab is None:
        return None
    post_key = str(row.get("post_key") or "")
    fbid = post_key[5:] if post_key.startswith("fbid:") else ""
    not_before = max(0, int(row.get("last_ts") or 0) // 1000 - 300)
    try:
        found = shared.confirm_new_post_permalink(
            CXT, tab, str(row.get("gid") or ""), fbid=fbid,
            not_before_ts=not_before)
    except Exception as e:
        log("reconcile: evidencia fallo gid=%s (%s)" % (row.get("gid"), e))
        return None
    url = str((found or {}).get("url") or "")
    # Regla dura: el permalink debe pertenecer al gid objetivo. Un "ok" de
    # otro grupo no acredita nada (seria un exito apuntado al grupo equivocado).
    if found.get("ok") and url and ("/groups/%s/" % row.get("gid")) in url:
        return {"proof": url}
    return None


def reconcile_ledger(evidence_fn=None, db=None, use_browser=True):
    """Resuelve los `inflight` de una ronda cortada, UNA sola pasada.

    Con evidencia afirmativa => ok + permalink. Sin evidencia => unknown, senal
    y NUNCA una nueva publicacion: la duda jamas se convierte en exito. Se llama
    al arrancar, antes de que el bucle pueda publicar.
    """
    rows = ledger.inflight(db=db)
    out = {"checked": len(rows), "ok": 0, "unknown": 0}
    for row in rows:
        evidence = None
        try:
            if evidence_fn is not None:
                evidence = evidence_fn(row)
            elif use_browser:
                evidence = _default_evidence(row, db=db)
        except Exception as e:
            log("reconcile err gid=%s %s" % (row.get("gid"), e))
        ts = ledger.now_ms()
        batch = str(row.get("batch") or "")
        try:
            if evidence:
                ledger.result(str(row.get("gid")), row.get("post_key"), "ok", 1,
                              evidence.get("proof") or "", batch, ts, db)
                ledger.ensure_group_attempt(str(row.get("gid")), ts, db=db)
                out["ok"] += 1
            else:
                ledger.result(str(row.get("gid")), row.get("post_key"), "unknown",
                              0, "", batch, ts, db)
                ledger.ensure_group_attempt(str(row.get("gid")), ts, db=db)
                ledger.note_signal(
                    "reconciliado_sin_evidencia",
                    "gid=%s post=%s" % (row.get("gid"), row.get("post_key")),
                    ts=ts, db=db)
                out["unknown"] += 1
        except Exception as e:
            log("reconcile write err gid=%s %s" % (row.get("gid"), e))
    return out


def reconcile_unknown(evidence_fn=None, db=None, use_browser=True, limit=20,
                      post_key="", gids=None):
    """Revisa parejas `unknown` buscando pruebas de lo YA publicado.

    Es solo lectura de Facebook: NUNCA publica ni reintenta. Si la vista de mis
    posts en el grupo muestra el permalink, la pareja sube a `ok` con evidencia;
    si no hay prueba se queda como estaba (no se inventa exito ni error). El
    llamador decide cuando lanzarlo: cuesta ~30 s por pareja.
    """
    rows = ledger.unresolved(limit=limit, post_key=post_key, gids=gids, db=db)
    out = {"checked": len(rows), "ok": 0, "left": 0, "errors": 0}
    for row in rows:
        evidence = None
        try:
            if evidence_fn is not None:
                evidence = evidence_fn(row)
            elif use_browser:
                evidence = _default_evidence(row, db=db)
        except Exception as e:
            out["errors"] += 1
            log("reconcile-unknown err gid=%s (%s)" % (row.get("gid"), e))
        url = str((evidence or {}).get("proof") or "")
        if not url:
            out["left"] += 1
            continue
        try:
            changed = ledger.upgrade(str(row.get("gid")), row.get("post_key"),
                                     url, ledger.now_ms(), db)
        except Exception as e:
            out["errors"] += 1
            log("reconcile-unknown write err gid=%s (%s)" % (row.get("gid"), e))
            continue
        if changed:
            out["ok"] += 1
            log("reconcile-unknown: gid=%s sube a ok (%s)"
                % (row.get("gid"), url))
    return out


def _reconcile_round_unknowns(applied, post_key, limit=None):
    """Intenta acreditar los `unknown` que acaba de dejar una ronda real.

    Un `unknown` es terminal para el selector: si la prueba fuerte fallo por un
    falso negativo, el item se marca error para siempre aunque el post exista
    (paso en la ronda real del grupo 636651664765406). Aqui se relee la vista
    member y, solo si aparece el permalink, la fila pasa a `ok`. NUNCA publica.
    """
    rows = [r for r in (applied.get("results") or [])
            if not r.get("ok") and r.get("status") in ("unknown", "pending")]
    targets = []
    for row in rows:
        gid = str(row.get("gid") or "")
        if gid and gid not in targets:
            targets.append(gid)
    cap = RECONCILE_UNKNOWN_MAX if limit is None else int(limit)
    targets = targets[:max(0, cap)]
    if not targets:
        return applied
    try:
        out = reconcile_unknown(post_key=post_key, gids=targets,
                                limit=len(targets))
    except Exception as e:
        log("ronda: no pude revisar unknown (%s)" % e)
        return applied
    if not out.get("ok"):
        log("ronda: %d unknown sin prueba tras revisar" % out.get("left", 0))
        return applied
    try:
        pm = ledger.post_group_map()
    except Exception as e:
        log("ronda: ledger ilegible tras revisar (%s)" % e)
        return applied
    proved = {}
    for row in rows:
        gid = str(row.get("gid") or "")
        cur = pm.get((gid, post_key)) or {}
        if gid in targets and cur.get("status") == "ok" and cur.get("proof"):
            row["ok"] = True
            row["status"] = "ok"
            row["proof"] = cur.get("proof")
            proved[gid] = cur.get("proof")
    if proved:
        applied["published"] = len([r for r in applied["results"] if r.get("ok")])
        applied["unresolved"] = [u for u in (applied.get("unresolved") or [])
                                if str(u) not in proved]
        log("ronda: %d unknown acreditado(s) por la vista member" % len(proved))
    return applied


def _ledger_maps():
    """Lee (grupos, post_group); si la BD no existe la crea y siembra una vez.

    El worker normal ya paso por `ledger_startup`, pero un test o un borrado
    accidental de ledger.db no deben tumbar la ronda: se reconstruye el esquema
    y se siembran los grupos de groups.json (sin pisar filas existentes).
    """
    try:
        return ledger.groups_map(), ledger.post_group_map()
    except Exception:
        ledger.init()
        if os.path.exists(GROUPS):
            ledger.seed_groups(GROUPS)
        return ledger.groups_map(), ledger.post_group_map()


def ledger_startup():
    """Prepara el ledger al arrancar: esquema, siembra, migracion y una pasada.

    `seed_groups` marca los grupos ya unidos como calientes (warm_at pasado):
    sin eso la primera ronda no tendria elegibles y el worker se pararia en
    silencio. La migracion de intentos se ejecuta una sola vez (senal
    'migrado') para no llenar la tabla de senales en cada reinicio.
    """
    try:
        ledger.init()
    except Exception as e:
        log("ledger init err " + str(e))
        return
    try:
        if os.path.exists(GROUPS):
            seeded = ledger.seed_groups(GROUPS)
            if seeded:
                log("ledger: %d grupo(s) sembrados" % seeded)
    except Exception as e:
        log("ledger seed err " + str(e))
    try:
        already = any(s.get("kind") == "migrado"
                      for s in ledger.signals(limit=100000))
        if not already:
            migrated = ledger.migrate_attempts(ATTEMPTS)
            if migrated:
                log("ledger: %d intento(s) migrados como senal" % migrated)
    except Exception as e:
        log("ledger migrate err " + str(e))
    enabled = False
    try:
        h = health()
        enabled = bool(h and h.get("enabled"))
    except Exception:
        enabled = False
    try:
        rec = reconcile_ledger(use_browser=enabled)
        if rec["checked"]:
            log("ledger: reconciliados %d inflight (ok=%d unknown=%d)"
                % (rec["checked"], rec["ok"], rec["unknown"]))
    except Exception as e:
        log("ledger reconcile err " + str(e))
    # Aviso (solo log) de un ledger vacio con publicaciones previas: ayuda a
    # detectar un historial perdido sin alterar la publicacion.
    try:
        warn_empty_ledger_history()
    except Exception as e:
        log("warn_empty_ledger_history err " + str(e))


def _ledger_db_ready(db=None):
    """True si se puede consultar el ledger sin crear/tocar el estado real."""
    if db is not None:
        return True
    try:
        return os.path.exists(ledger.db_path())
    except Exception:
        return False


def ledger_attempted_today(db=None):
    """Gasto del dia segun el ledger; None si no hay BD/consulta fiable (§3.3)."""
    if not _ledger_db_ready(db):
        return None
    try:
        return ledger.attempted_today(db=db)
    except Exception as e:
        log("ledger attempted_today err " + str(e))
        return None


def warn_empty_ledger_history(db=None):
    """Aviso de arranque (solo log): ledger vacio con publicaciones previas.

    Si `post_group` no tiene NINGUNA fila pero `autopost_attempts.json` guarda
    items con `published>0`, el historial de cobertura se perdio (p. ej.
    `ledger.db` recreado) y el worker publico antes sin dejar rastro. Escribe
    una linea `WARN historial_vacio: N ...` y devuelve N (suma de `published`);
    con una BD inexistente o con filas devuelve 0 sin avisar. NO cambia el
    comportamiento: nunca aborta ni altera la publicacion.
    """
    if not _ledger_db_ready(db):
        return 0
    try:
        if ledger.post_group_map(db=db):
            return 0
    except Exception as e:
        log("warn_empty_ledger_history err " + str(e))
        return 0
    published = 0
    for value in load_attempts().values():
        if isinstance(value, dict):
            try:
                published += int(value.get("published") or 0)
            except (TypeError, ValueError):
                continue
    if published > 0:
        log("WARN historial_vacio: %d publicaciones previas sin registrar en ledger"
            % published)
    return published


# ------------------------------------------------------------ parada dura

def write_halt(reason, now=None, hours=None):
    """Escribe la parada dura (§3.7): no se publica hasta `until`.

    Best-effort: un fallo de disco no debe tumbar la ronda; el llamador ya
    marco el status como pausado.
    """
    now = int(now if now is not None else time.time() * 1000)
    h = HALT_HOURS if hours is None else float(hours)
    payload = {"until": now + int(h * 3600 * 1000),
               "reason": str(reason or "")[:200], "at": now}
    try:
        cxtlib.write_json_atomic(HALT, payload)
    except Exception as e:
        log("halt write err " + str(e))
    log("PARADA DURA hasta %s: %s"
        % (time.strftime("%Y-%m-%d %H:%M:%S UTC",
                         time.gmtime(payload["until"] / 1000.0)),
           payload["reason"]))


def active_halt(now=None):
    """Devuelve el halt vigente o {} si no hay/ya vencio.

    P5 fail-closed: si el archivo EXISTE pero no se puede leer, no es un objeto
    JSON o su `until` falta/es invalido, se considera halt ACTIVO y se registra
    `halt_ilegible`. Un fallo de disco jamas puede desactivar la parada dura.
    """
    now = int(now if now is not None else time.time() * 1000)
    if not os.path.exists(HALT):
        return {}
    try:
        with open(HALT, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, TypeError) as e:
        _note_signal_quiet("halt_ilegible", "%s: %s" % (type(e).__name__, e))
        return {"until": now, "reason": "halt ilegible"}
    if not isinstance(raw, dict) or not raw:
        _note_signal_quiet("halt_ilegible", "no es objeto JSON")
        return {"until": now, "reason": "halt ilegible"}
    try:
        until = int(raw.get("until"))
    except (TypeError, ValueError):
        _note_signal_quiet("halt_ilegible", "until invalido")
        return {"until": now, "reason": "halt ilegible"}
    return raw if until > now else {}


def clear_halt():
    """Borra la parada dura (CLI --clear-halt). Devuelve True si existia."""
    try:
        os.remove(HALT)
        return True
    except OSError:
        return False


# ------------------------------------------------------------ bucle

def _pid_alive():
    try:
        with open(PID, encoding="utf-8") as f:
            old = int(f.read().strip())
    except Exception:
        return False
    try:
        os.kill(old, 0)
        return True
    except OSError:
        return False


def _write_pid():

    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".autopost.pid.", suffix=".tmp", dir=DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, PID)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _remove_pid():
    try:
        os.remove(PID)
    except OSError:
        pass


def _on_term(*_):
    # §3.4: matar al hijo ANTES de que el `finally` de _publish libere el
    # browser.lock; si no, otro driver puede publicar mientras el hijo sigue
    # vivo sobre el mismo navegador.
    _kill_child()
    _remove_pid()
    raise SystemExit(0)


def _label(item):
    return (item.get("url") or item.get("expect") or item.get("text") or "")[:80]


_ITEM_FIELDS = ("url", "expect", "filter", "limit", "text", "dry", "fast", "composer")


def public_items(items):
    """Vista resuelta de la cola para el popup: mezcla el item (con su estado ya
    reconciliado por recover_states) con published/tries del registro de
    intentos. Sin esto el popup solo veia items crudos sin estado."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        d = {k: it.get(k) for k in _ITEM_FIELDS if it.get(k) is not None}
        # La UI muestra el texto saneado y conserva el original si lo trae.
        if it.get("expect") is not None or it.get("expect_raw") is not None:
            raw_expect = it.get("expect_raw") or it.get("expect")
            d["expect"] = clean_expect(raw_expect)
            if it.get("expect_raw") is not None:
                d["expect_raw"] = it.get("expect_raw")
        # P6: el status SIEMPRE declara la simulacion; un item sin `dry` no
        # puede aparecer en la UI como publicacion real.
        d["dry"] = is_dry(it)
        d["state"] = it.get("state") or "pendiente"
        d["error"] = it.get("error") or ""
        d["published"] = int(it.get("published", 0) or 0)
        d["tries"] = int(it.get("tries", 0) or 0)
        if it.get("at"):
            d["at"] = it.get("at")
        out.append(d)
    return out


def _sync_items(st, q):
    st["items"] = public_items((q or {}).get("items") or [])
    return st


def _refresh_pending(st, items=None):
    if items is None:
        items = read_queue().get("items") or []
    st["pending"] = count_pending(items)


def _round_mode(item, dry):
    """Etiqueta `last_run.mode`: dry manda sobre fast (plan 5.6)."""
    if dry:
        return "dry"
    return fastmode.tag(item=item)


def _run_id(now, batch=""):
    """Id estable de la ronda: batch si existe, si no reloj+azar."""
    token = "".join(ch for ch in str(batch or "") if ch.isalnum() or ch in "-_")
    return "run-%d-%s" % (int(now), token or ("%04d" % random.randint(0, 9999)))


def last_run_message(mode, shared, total, failed):
    """Texto EXACTO de plan 5.6 para el resumen final.

    Dry-run nunca dice que se compartio. `exitosamente` (variante permitida)
    solo aplica con failed=0; aqui el camino ok usa "Se compartió en N grupos".
    """
    if mode == "dry":
        return "Prueba completada: %d grupos (sin publicar)." % total
    if total <= 0 or shared <= 0:
        return "No se pudo compartir. Revisa la alerta."
    if failed == 0 and shared >= total:
        return "¡Listo! Se compartió en %d grupos." % shared
    return "Se compartió en %d de %d grupos; %d con alerta." % (
        shared, total, max(0, total - shared))


def build_last_run(run_id, started_at, applied, gids, mode, profile="",
                   profile_name="", finished_at=None, dry=False, error="",
                   metrics=None, names=None):
    """Arma `last_run` (plan 5.6) desde el resultado POR GRUPO de la ronda.

    Cuenta sobre TODOS los gids pedidos (no sobre la lista capada): `shared` =
    status ok, `skipped` = pending (espera aprobacion) y `failed` = el resto
    (unknown/blocked/removed/sin evidencia). `items` se capa a
    LAST_RUN_ITEMS_CAP. Los nombres de grupo son opcionales (`names`): con
    ledger vacio se usa el gid, que es el identificador estable.
    """
    finished_at = int(finished_at if finished_at is not None
                      else time.time() * 1000)
    gids = [str(g) for g in (gids or [])]
    rows = [r for r in ((applied or {}).get("results") or [])
            if isinstance(r, dict)]
    by_gid = {str(r.get("gid") or ""): r for r in rows}
    if gids:
        ordered = [(g, by_gid.get(g) or {}) for g in gids]
    else:
        ordered = [(str(r.get("gid") or ""), r) for r in rows]
    total = len(ordered)
    shared = 0
    skipped = 0
    for _gid, row in ordered:
        status = str(row.get("status") or "")
        if bool(row.get("ok")):
            shared += 1
        elif status == "pending":
            skipped += 1
    failed = max(0, total - shared - skipped)
    names = names or {}
    items = []
    for gid, row in ordered[:LAST_RUN_ITEMS_CAP]:
        ok = bool(row.get("ok"))
        status = str(row.get("status") or "")
        items.append({
            "group": str(names.get(gid) or gid),
            # share_post no devuelve la URL del grupo: "" en vez de inventarla.
            "url": str(row.get("url") or ""),
            "ok": ok,
            "post_url": str(row.get("proof") or "") if ok else "",
            "reason": "" if ok else (str(row.get("reason") or "")
                                     or status or "sin resultado"),
        })
    ok = (not error and total > 0 and shared == total
          and failed == 0 and skipped == 0)
    if dry:
        ok = not error
    metrics = dict(metrics or {})
    for key in ("enqueue_to_first_action_ms", "group_total_ms",
                "cxt_calls_per_group", "status_bytes", "status_write_ms",
                "lock_wait_ms"):
        metrics.setdefault(key, None)
    metrics["duration_ms"] = max(0, finished_at - int(started_at or 0))
    metrics["publish_success"] = shared
    metrics["publish_failed"] = failed
    metrics["publish_skipped"] = skipped
    return {
        "id": run_id,
        "profile": profile or "",
        "profile_name": profile_name or "",
        "started_at": int(started_at or 0),
        "finished_at": finished_at,
        "mode": mode,
        "ok": bool(ok),
        "shared": shared,
        "failed": failed,
        "skipped": skipped,
        "items": items,
        "message": last_run_message(mode, shared, total, failed),
        "metrics": metrics,
    }


def _write_status_measured(st):
    """Escribe el status y devuelve (ms, bytes) de ESA escritura."""
    t0 = time.perf_counter()
    write_status(st)
    ms = max(0, int(round((time.perf_counter() - t0) * 1000)))
    try:
        size = os.path.getsize(STATUS)
    except OSError as e:
        log("status size err " + str(e))
        size = None
    return ms, size


def _persist_round(st, lr):
    """Persiste `last_run` y completa las metricas de la escritura del status.

    La primera escritura ya lleva el resumen; la segunda solo agrega los dos
    valores que se conocen al medir la primera (bytes y milisegundos).
    """
    st["last_run"] = lr
    ms, size = _write_status_measured(st)
    lr["metrics"]["status_write_ms"] = ms
    lr["metrics"]["status_bytes"] = size
    write_status(st)


def _last_run_names(gids):
    """Nombres de grupo del ledger (best-effort); sin ledger se usa el gid."""
    if not gids:
        return {}
    try:
        return {str(g): (row or {}).get("name") or str(g)
                for g, row in (_ledger_maps()[0] or {}).items()}
    except Exception as e:
        log("last_run sin nombres de grupo: " + str(e))
        return {}


def _record_last_run(st, item, gids, applied, started_at, batch, dry, age_ms,
                     lock_wait_ms, res):
    """Deja `last_run` (plan 5.6) en el status, aun sin escribir el archivo.

    Solo se llama cuando la ronda EMIZO (share/ledger): los abortos previos
    (navegador ocupado, intento terminal) no son una ejecucion y no pisan el
    resumen anterior.
    """
    pin = profile_pin(st)
    st["profile_dir"] = pin["profile_dir"]
    st["profile_name"] = pin["profile_name"]
    tab = _dedicated_tab()
    if tab is not None:
        st["auto_tab"] = tab
    metrics = {
        # Edad de la cola al cerrar la ronda: la misma cifra del log.
        "enqueue_to_first_action_ms": age_ms,
        # TODO(v1.5): share_post no reporta tiempos por grupo; sin esos datos
        # p50/p95 no se pueden calcular y no se estiman (plan 9.1/9.4).
        "group_total_ms": None,
        "cxt_calls_per_group": None,
        "lock_wait_ms": lock_wait_ms,
        "status_bytes": None,
        "status_write_ms": None,
    }
    return build_last_run(
        _run_id(started_at, batch), started_at, applied, gids,
        _round_mode(item, dry),
        profile=pin["profile_dir"], profile_name=pin["profile_name"],
        finished_at=int(time.time() * 1000), dry=dry,
        error=str((res or {}).get("error") or ""), metrics=metrics,
        names=_last_run_names(gids))


def _publish(st, q, items, item, now, gids=None, post_key=""):
    """Publica UNA ronda del item en los `gids` ya elegidos por el selector.

    El orden de las escrituras es el invariante central de F1:
      1. `ledger.intent` (inflight) ANTES de escribir el cmd de la ronda.
      2. `share_post` publica y reporta evidencia por gid.
      3. `ledger.result` por grupo al terminar (o al timeout, con lo parcial).
    Si el subproceso muere sin status, las filas quedan inflight y se resuelven
    en el siguiente arranque; jamas se re-publica a ciegas.

    `gids=None` es el camino heredado (tests/CLI directa): no toca el ledger.
    Un dry-run tampoco escribe el ledger: no acredita publicaciones reales.
    Devuelve un resumen de la ronda (el llamador decide la cobertura, §3.5).
    """
    dry = is_dry(item)
    fast = fastmode.enabled(item=item)
    key = item_key(item, dry=dry)
    gid_list = [str(g) for g in (gids or [])]
    post_key = post_key or ""
    lock_fh = None
    lock_wait_ms = None
    bad = bad_item(item)
    out = {"state": "error", "published": 0, "pending": 0, "unresolved": 0,
           "dry": dry, "gids": gid_list, "aborted": False}

    pin = profile_pin(st)
    if not pin["available"] and pin["profile_dir"]:
        # Perfil fijado inexistente: se alerta y NO se publica ni se escribe
        # intent en el ledger (un cambio de perfil silencioso esta prohibido).
        msg = "perfil no disponible: " + pin["profile_dir"]
        item["state"] = "error"
        item["error"] = msg
        st["error"] = "ALERTA: " + msg
        st["current"] = None
        interval = effective_interval(item, q)
        st["interval_min"] = interval
        st["next_at"] = next_at_for(now, interval, fast=fast)
        _sync_items(st, q)
        log("no publico: " + msg)
        lr = build_last_run(_run_id(now, ""), now, {"results": []}, gid_list,
                            _round_mode(item, dry),
                            profile=pin["profile_dir"],
                            profile_name=pin["profile_name"],
                            dry=dry, error=msg)
        _persist_round(st, lr)
        out["state"] = "error"
        return out

    if gid_list and not bad:
        t_lock = time.perf_counter()
        try:
            lock_fh = ledger.lock_browser(LOCK)
        except Exception as e:
            log("lock err " + str(e))
            lock_fh = None
        lock_wait_ms = int(round((time.perf_counter() - t_lock) * 1000))
        if lock_fh is None:
            # Otro driver (popup/CLI/worker) tiene el navegador: no se publica
            # nada y se dice. No cuenta intento (`tries` intacto) porque la
            # ronda no empezo; next_at evita reintentar el lock en bucle.
            item["state"] = "error"
            item["error"] = "browser ocupado por otro driver"
            st["error"] = "browser ocupado por otro driver"
            st["current"] = None
            interval = effective_interval(item, q)
            st["interval_min"] = interval
            st["next_at"] = next_at_for(now, interval, fast=fast)
            _sync_items(st, q)
            log("browser ocupado por otro driver: no publico")
            write_status(st)
            out["state"] = "error"
            return out
        # §3.4: con el lock YA tomado se releen los intentos. Si otro driver
        # cerro este item mientras esperabamos, abortar sin publicar.
        prev = load_attempts().get(key) or {}
        if _attempt_terminal(prev):
            ledger.unlock_browser(lock_fh)
            lock_fh = None
            terminal_ok = (str(prev.get("state") or "") in ("hecho", "publicando")
                           or int(prev.get("published", 0) or 0) > 0)
            item["state"] = "hecho" if terminal_ok else "error"
            item["error"] = "intento terminal ya registrado"
            st["current"] = None
            st["error"] = ""
            st["pending"] = count_pending(items)
            _sync_items(st, q)
            log("aborto: %s ya tenia intento terminal" % _label(item))
            write_status(st)
            out["state"] = item["state"]
            out["aborted"] = True
            return out

    attempt_mark(key, "publicando")
    item["state"] = "publicando"
    item["error"] = ""
    item["at"] = item.get("at") or now
    st["dom"]["tagged"] = int(st["dom"].get("tagged", 0)) + 1
    st["current"] = _label(item)
    st["error"] = ""
    write_status(st)

    published = 0
    pend = 0
    unres = 0
    # P3/FIX: el batch debe existir ANTES del `try`. Si `run_share` lanza, el
    # `except` sella las filas `inflight` de ESTE lote; sin la variable fuera
    # del try quedaban inflight para siempre (y se re-publicaban al dia UTC
    # siguiente). Vacio = aun no se abrio la ronda, no hay nada que sellar.
    batch = ""
    applied = {"results": [], "published": 0, "pending": 0, "unresolved": []}
    if bad:
        res = {"ok": False, "error": bad}
    else:
        item["_gids"] = gid_list
        item["_post_key"] = post_key
        item["_lock_held"] = bool(lock_fh is not None)
        item["_batch"] = ""
        try:
            batch = "b%d-%04d" % (now, random.randint(0, 9999))
            item["_batch"] = batch
            if gid_list and not dry:
                ledger.intent(intent_rows(gid_list, post_key, now), batch=batch)
            raw = run_share(item)
            st["dom"]["injected"] = int(st["dom"].get("injected", 0)) + 1
            applied = apply_round_results(gid_list, post_key, raw, batch=batch,
                                          ts=now, dry=dry)
            if not dry and post_key and gid_list:
                applied = _reconcile_round_unknowns(applied, post_key)
            published = max(int((raw or {}).get("published", 0) or 0),
                            int(applied.get("published", 0) or 0))
            pend = int(applied.get("pending", 0) or 0)
            unres = len(applied.get("unresolved") or [])
            unres += len([r for r in (applied.get("results") or [])
                          if r.get("status") in ("unknown", "pending")])
            state, err = round_outcome(raw, gid_list, applied)
            res = {"ok": state == "hecho", "error": err}
        except Exception as e:
            # P3: la ronda murio sin status. Sellar el lote evita que las
            # filas queden `inflight` y se re-publicen al dia UTC siguiente.
            # Es idempotente: solo toca `inflight` de este `batch`.
            if batch and gid_list and not dry:
                try:
                    ledger.seal_partial(batch, gids=gid_list, now=now)
                except Exception as e2:
                    log("ledger seal_partial err " + str(e2))
            res = {"ok": False, "error": str(e)[:200]}
        finally:
            # Campos transitorios: no deben viajar a status ni a autoqueue.
            item.pop("_gids", None)
            item.pop("_post_key", None)
            item.pop("_lock_held", None)
            item.pop("_batch", None)
            ledger.unlock_browser(lock_fh)

    item["state"] = "hecho" if res.get("ok") else "error"
    item["error"] = res.get("error") or ""
    if unres > 0 and item["state"] == "error":
        item["error"] = "%d sin prueba (no se reintenta)" % unres
    item["done_ms"] = int(time.time() * 1000)
    attempt_mark(key, item["state"], item["error"], published=published,
                 pending=pend, unresolved=unres)
    if not dry and any(r.get("status") in ("blocked", "removed")
                       for r in (applied.get("results") or [])):
        note_alert(st, "bloqueo/eliminacion en %d grupo(s)"
                   % len([r for r in applied["results"]
                          if r.get("status") in ("blocked", "removed")]), now)

    st["results"] = (st.get("results") or [])[-99:] + [{
        "url": item.get("url") or "", "state": item["state"],
        "ok": bool(res.get("ok")), "error": item["error"],
        "pending": pend, "published": published,
        "mode": fastmode.tag(item=item),
        "at": int(time.time() * 1000)}]
    st["done"] = int(st.get("done", 0)) + 1
    # Un dry-run no consume el tope anti-ban. Una publicacion real suma GRUPOS,
    # no items: una ronda de 10 grupos gasta 10 del techo diario. La fuente
    # autoritativa es el ledger (`attempted_today`), que tambien cuenta unknown.
    if not dry:
        att = ledger_attempted_today()
        if att is not None and gid_list:
            st["done_today"] = int(att.get("attempted") or 0)
        else:
            st["done_today"] = int(st.get("done_today", 0)) + int(published)
    if not res.get("ok"):
        st["dom"]["errors"] = int(st["dom"].get("errors", 0)) + 1
    st["current"] = None
    interval = effective_interval(item, q)
    st["interval_min"] = interval
    # §3.6: la espera nunca es menor que lo que duro la ronda (N pausas), o la
    # siguiente ronda se solaparia con la anterior sobre el mismo navegador.
    st["next_at"] = max(next_at_for(now, interval, fast=fast),
                        int(now) + round_duration_ms(len(gid_list), fast=fast))
    st["pending"] = count_pending(items)
    try:
        # ms desde que el daemon escribio la cola (enqueue) hasta esta accion:
        # metrica de "enqueue -> primera accion". En la espera ENTRE items
        # refleja el intervalo (como debe ser).
        age = int(time.time() * 1000) - int(os.path.getmtime(QUEUE) * 1000)
    except OSError:
        age = -1
    log("item %s ok=%s %s (cola->accion %dms)" % (item["state"], res.get("ok"), _label(item), age))
    if pend:
        log("AVISO: %d grupo(s) quedaron pendientes de aprobacion; item sin reintento automatico" % pend)
    lr = _record_last_run(st, item, gid_list, applied, now, batch, dry, age,
                          lock_wait_ms, res)
    _persist_round(st, lr)
    out.update({"state": item["state"], "published": published,
                "pending": pend, "unresolved": unres})
    return out


def _tick(st, cmd_path, sig=None, db=None):
    q = read_queue()
    _sync_items(st, q)
    # Perfil fijado del job: visible en el status desde el primer tick aunque
    # todavia no haya ronda (y estable durante toda la vida del worker).
    pin = profile_pin(st)
    st["profile_dir"] = pin["profile_dir"]
    st["profile_name"] = pin["profile_name"]
    # La cola es la fuente de verdad de la pausa operativa: el daemon escribe
    # q.paused (popup PAUSAR/REANUDAR). Se sincroniza cada tick, antes del gate.
    st["pausado"] = bool(q.get("paused"))

    cmd = consume_cmd(cmd_path)
    now = int(time.time() * 1000)
    if cmd.get("stop"):
        log("stop (cmd)")
        return "stop"
    # El daemon SI deja autopost.cmd.json para next/retry (verificado): el popup
    # manda autopostSet y el daemon escribe el cmd; el worker lo consume aqui.
    # El `pause` por cmd (ruta manual legacy) solo sobreescribe la pausa del tick.
    if cmd.get("action") == "pause":
        st["pausado"] = bool(cmd.get("value"))
        log("cmd pause=%s" % st["pausado"])
    if cmd.get("action") == "next":
        # SIGUIENTE (popup): adelanta la proxima publicacion. P7: nunca a 0;
        # el suelo de 60 s evita colapsar la pausa anti-ban de 4-8 min.
        # ASIGNAR, no max(): con max() un next_at ya programado (interval_min
        # 15 min) nunca baja y el boton SIGUIENTE no hacia nada. El suelo de
        # 60 s sigue intacto (nunca 0).
        st["next_at"] = now + KICK_MIN_MS
        log("cmd next: forzar ahora")
    if cmd.get("action") == "retry":
        # REINTENTAR (popup): rearma un item fallido borrando su intento de error
        # y adelanta la publicacion. Un intento "hecho" no se borra (clear_attempt).
        u = cmd.get("url") or ""
        if u and clear_attempt(u):
            # Igual que SIGUIENTE: reasignar (max() dejaba la ronda ya
            # programada en su hora y el reintento no adelantaba nada).
            st["next_at"] = now + KICK_MIN_MS
            log("cmd retry: %s" % u[:60])

    # Senal de cola: el daemon reescribio autoqueue.json (enqueue/del/clear/
    # pausa/override de intervalo). Se re-evalua YA, sin esperar el next_at: el
    # intervalo es la espera ENTRE items, no un retardo de arranque.
    kick = bool(cmd)
    if sig is not None:
        qs = _queue_sig(q)
        if sig.get("sig") is not None and qs != sig["sig"]:
            st["next_at"] = max(int(st.get("next_at") or 0), now + KICK_MIN_MS)
            kick = True
            log("senal de cola: reevaluar ahora")
        sig["sig"] = qs

    items = q.get("items") or []
    # Dia UTC: el dia local se desfasa con DST y estanca el contador anti-ban.
    roll_day(st, ledger.utc_day(now))
    # §3.3: el tope se deriva del LEDGER, no del status file. Tambien cuenta
    # unknown: un fallo sin evidencia gasto un intento real.
    att = ledger_attempted_today(db=db)
    if att is not None:
        st["done_today"] = int(att.get("attempted") or 0)
    due = ((not st.get("pausado")) and under_daily_cap(st.get("done_today", 0))
           and count_pending(items) > 0 and now >= int(st.get("next_at", 0) or 0))
    # Camino CARO (health + tab + checkpoint + share): solo cuando hace falta.
    # Ocioso corre cada HEAVY_MS para no spamear Chrome; una senal, un cmd o
    # trabajo vencido lo disparan al instante. NUNCA se publica sin health fresco.
    if sig is None:
        heavy = True
    else:
        heavy = kick or due or (now - int(sig.get("heavy", 0) or 0) >= HEAVY_MS)
    if not heavy:
        return
    # §3.8: `heavy` se marca SOLO cuando el paso caro corre de verdad; antes se
    # reescribia en cada tick y el chequeo de salud a 1 Hz nunca llegaba a
    # cumplirse.
    if sig is not None:
        sig["heavy"] = now

    # §3.7: parada dura. Mientras `until` este en el futuro NO se publica nada,
    # ni siquiera se consulta el navegador. Se levanta con --clear-halt.
    halt = active_halt(now)
    if halt:
        st["pausado"] = True
        st["current"] = None
        st["error"] = "parada dura hasta %s (%s)" % (
            time.strftime("%Y-%m-%d %H:%M:%S UTC",
                          time.gmtime(int(halt.get("until") or 0) / 1000.0)),
            halt.get("reason") or "")
        _refresh_pending(st, items)
        write_status(st)
        return

    h = health()
    if h is None:
        st["pausado"] = True
        st["current"] = None
        st["error"] = "daemon no responde"
        _refresh_pending(st)
        write_status(st)
        return
    if not h.get("enabled"):
        st["pausado"] = True
        st["current"] = None
        log("pausado (kill switch)")
        _refresh_pending(st)
        write_status(st)
        return
    if st.get("error") == "daemon no responde":
        st["error"] = ""

    # La pausa debe cortar ANTES de tocar Chrome: si no, el worker crea/navega
    # la pestaña de automatización y consulta checkpoints cada HEAVY_MS aunque
    # el usuario lo haya pausado. Es una operación observable y evitable.
    if st.get("pausado"):
        st["current"] = None
        _refresh_pending(st, items)
        write_status(st)
        return

    job = "share" if h.get("share_alive") else ("share_post" if h.get("share_post_alive") else "")
    tab = _dedicated_tab()
    if tab is not None:
        st["auto_tab"] = tab
    cp = checkpoint_now(tab)
    if cp:
        if cp == "block":
            note_alert(st, "aviso de bloqueo en dialogo", now)
        elif cp == "dialog":
            note_alert(st, "aviso de bloqueo en dialogo"
                       if block_notice(tab) else "aviso en dialogo", now)
        else:
            apply_checkpoint(st, cp)
        st["current"] = None
        log("alerta: revise la cuenta; la cola sigue activa")
        _refresh_pending(st)
        write_status(st)
        return
    if job:
        set_waiting(st, job)
        _refresh_pending(st)
        write_status(st)
        return
    clear_waiting(st)
    if st.get("pausado"):
        st["current"] = None
        _refresh_pending(st)
        write_status(st)
        return

    _refresh_pending(st, items)
    if not under_daily_cap(st.get("done_today", 0)):
        st["error"] = "tope diario alcanzado"
        write_status(st)
        return
    if st.get("error") == "tope diario alcanzado":
        st["error"] = ""
    if now < int(st.get("next_at", 0) or 0):
        write_status(st)
        return
    # §3.3: una cuarentena vencida vuelve a `ok` al inicio de la ronda; sin
    # esto `blocked` era permanente y el grupo nunca servia para otra
    # publicacion.
    try:
        if _ledger_db_ready(db):
            expired = ledger.expire_quarantine(now=now, db=db)
            if expired:
                log("ledger: %d cuarentena(s) caducadas" % expired)
    except Exception as e:
        log("expire_quarantine err " + str(e))

    # §3.5 cabeza de fila: un item diferido no puede frenar a los demas. Se
    # recorren los pendientes del tick como maximo una vez cada uno.
    pending_now = [it for it in items if (it.get("state") or "pendiente") == "pendiente"]
    deferred_next = None
    rel = load_relevance()
    for _ in range(len(pending_now)):
        item = pick_pending(items, now)
        if item is None:
            break
        post_key = ledger.post_key(item.get("url") or "", item.get("fbid") or "")
        # Cupo restante de HOY en grupos: min(MAXDAY - intentados, MAXGROUPS).
        daily_left = max(0, min(MAXGROUPS, MAXDAY - int(st.get("done_today", 0))))
        try:
            groups_map, post_map = _ledger_maps()
            if not groups_map and os.path.exists(GROUPS):
                ledger.seed_groups(GROUPS)
                groups_map = ledger.groups_map()
            # Cobertura ya cubierta: no se re-publica una pareja `ok` solo
            # porque paso COOLDOWN_POST mientras el item seguia abierto.
            cov = coverage_state(groups_map, post_map, post_key, rel)
            # `limit` del item es el tope POR RONDA del usuario: sin pasarlo,
            # select_round usaba MAXGROUPS (10) y una cola con limit:3 publicaba
            # en 10 grupos por vuelta (visto 2026-11-08, 00:38).
            selection = select_round(groups_map, post_map, post_key, now,
                                     daily_left=daily_left, rel=rel,
                                     exclude_gids=cov["covered_gids"],
                                     maxgroups=group_limit(item.get("limit")),
                                     fast=fastmode.enabled(item=item))
        except Exception as e:
            # El ledger es el selector: si no se puede leer NO se publica a ciegas.
            st["error"] = "ledger no disponible: " + str(e)[:120]
            log("ledger err " + str(e))
            write_status(st)
            return

        for row in selection.get("discarded") or []:
            if str(row.get("reason") or "").startswith(("token_negativo", "sin_relevancia")):
                log("relevancia: %s descartado (%s)"
                    % (row.get("name") or row.get("gid"), row.get("reason")))
        if selection.get("clock_jump"):
            try:
                ledger.note_signal("clock_jump", "post=%s" % post_key)
            except Exception as e:
                log("ledger clock_jump err " + str(e))

        if selection.get("exhausted"):
            # La publicacion YA alcanzo a todos los grupos relevantes: se cierra
            # sin volver a publicar (aqui cae tambien un item rearmado a mano).
            item["state"] = "hecho"
            item["error"] = ""
            item["done_ms"] = int(time.time() * 1000)
            attempt_mark(item_key(item, dry=is_dry(item)), "hecho",
                         "", unresolved=0)
            log("cobertura completa (%d grupos): item hecho"
                % int(selection.get("relevant") or 0))
            continue

        if selection.get("deferred"):
            # 0 elegibles NO es error: el item se difiere con un next_at
            # calculado y `tries` intacto. Marcarlo error lo mataba (el bug que
            # congelaba la cobertura). El estado se refleja solo en
            # memoria/status; la cola sigue siendo propiedad del daemon.
            item["state"] = "diferido"
            item["error"] = ""
            st["error"] = ""
            st["deferred"] = int(st.get("deferred", 0) or 0) + 1
            cand = max(int(now) + 60000, int(selection.get("next_at") or 0))
            deferred_next = cand if deferred_next is None else min(deferred_next, cand)
            try:
                ledger.note_signal("diferido", "post=%s next=%d elegibles=%d"
                                   % (post_key, cand,
                                      int(selection.get("eligible") or 0)))
            except Exception as e:
                log("ledger diferido err " + str(e))
            log("diferido: 0 grupos elegibles; proximo %d" % cand)
            continue

        out = _publish(st, q, items, item, now, gids=selection["gids"],
                       post_key=post_key)
        # §3.5: si la ronda salio bien pero aun quedan grupos relevantes sin
        # cubrir, el item SIGUE pendiente para la ronda siguiente (no se cierra
        # con 10 de 20). Solo la cobertura completa lo marca `hecho`.
        if (out and not is_dry(item) and out.get("state") == "hecho"
                and out.get("gids") and not out.get("aborted")):
            try:
                gm, pm = _ledger_maps()
                cov = coverage_state(gm, pm, post_key, rel)
            except Exception as e:
                log("coverage err " + str(e))
                cov = {"exhausted": True, "covered_gids": [], "relevant": 0}
            if not cov.get("exhausted"):
                item["state"] = "pendiente"
                item["error"] = ""
                attempt_mark(item_key(item), "pendiente", "",
                             published=out.get("published", 0), pending=0,
                             unresolved=0)
                st["next_at"] = max(int(st.get("next_at") or 0),
                                    int(now) + round_duration_ms(len(out["gids"])))
                log("cobertura %d/%d: el item sigue pendiente"
                    % (len(cov.get("covered_gids") or []), int(cov.get("relevant") or 0)))
            _refresh_pending(st, items)
            _sync_items(st, q)
            write_status(st)
        return

    st["pending"] = count_pending(items)
    if deferred_next is not None:
        st["next_at"] = deferred_next
    _sync_items(st, q)
    write_status(st)


def _loop(st, cmd_path):
    # Firma base de la cola: el estado inicial NO cuenta como "senal" (asi el
    # arranque conserva RE4); cualquier reescritura posterior si.
    sig = {"sig": None, "heavy": 0}
    try:
        sig["sig"] = _queue_sig(read_queue())
    except Exception:
        sig["sig"] = None
    while True:
        try:
            if _tick(st, cmd_path, sig) == "stop":
                return
        except Exception as e:
            st["error"] = str(e)[:200]
            log("loop err " + str(e))
            write_status(st)
        time.sleep(LOOP_SLEEP)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", default="")
    ap.add_argument("--clear-halt", action="store_true",
                    help="borra autopost.halt.json (parada dura) y sale")
    ap.add_argument("--reconcile-unknown", action="store_true",
                    help="revisa parejas unknown con evidencia ya publicada"
                         " (solo lectura, no publica) y sale")
    ap.add_argument("--limit", type=int, default=20,
                    help="maximo de parejas unknown a revisar")
    a = ap.parse_args(argv)
    cmd_path = os.path.abspath(a.cmd) if a.cmd else CMD

    if a.clear_halt:
        existed = clear_halt()
        log("halt %s" % ("borrado" if existed else "no existia"))
        return

    if a.reconcile_unknown:
        out = reconcile_unknown(limit=max(1, int(a.limit)))
        print(json.dumps(out, ensure_ascii=False))
        return

    # §3.4: un solo worker aunque `autopost.pid` se haya perdido. El flock vive
    # lo que vive el proceso y no se puede heredar por accidente.
    worker_fh = ledger.lock_browser(WORKER_LOCK)
    if worker_fh is None:
        log("ya hay otro worker (autopost.lock)")
        return
    if _pid_alive():
        log("ya hay un autopost vivo pid=%s" % PID)
        ledger.unlock_browser(worker_fh)
        return
    _write_pid()
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    st = load_status()
    st["running"] = True
    st["pausado"] = bool(read_queue().get("paused"))
    st["started_ms"] = int(time.time() * 1000)
    st["finished_ms"] = 0
    st["current"] = None
    st["error"] = ""
    now = int(time.time() * 1000)
    prev_next = int(st.get("next_at", 0) or 0)
    st["next_at"] = startup_next_at(now, prev_next)
    if prev_next and st["next_at"] != prev_next:
        log("RE4: next_at vencido -> reprogramo a ahora+REC")
    q = read_queue()
    _sync_items(st, q)
    _refresh_pending(st, q.get("items") or [])
    write_status(st)
    log("autopost arranca pid=%d" % os.getpid())
    # Reconciliacion UNICA antes de permitir cualquier publicacion: un corte a
    # mitad de ronda deja filas inflight; se resuelven por evidencia (o unknown)
    # y nunca con un reintento ciego.
    ledger_startup()
    try:
        _loop(st, cmd_path)
    finally:
        st["running"] = False
        st["current"] = None
        st["finished_ms"] = int(time.time() * 1000)
        write_status(st)
        _remove_pid()


if __name__ == "__main__":
    main()
