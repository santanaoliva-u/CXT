#!/usr/bin/env python3
"""QA de integracion (SIN navegador, SIN daemon) del contrato F1 congelado.

Reproduce los 8 escenarios del §5/§10 de `work/f1_interfaces.md` (F1-1..F1-8)
contra el codigo REAL de `autopost.py` y `ledger.py`, sin abrir Chrome ni el
daemon y sin tocar el estado vivo del usuario:

  F1-1 ledger sembrado (blocked con TTL futuro / warm_at futuro / ok) -> la
       ronda elige EXACTAMENTE 1 gid y `autopost_attempts.json` no cambia.
  F1-2 corte a mitad de ronda (3 `intent`, ningun `result`) -> los 3 `inflight`
       se reconcilian y NO se re-publica nada (cero nuevas intenciones).
  F1-3 reintento de un item con `published>0` -> no re-publica los hechos y si
       cubre los pendientes.
  F1-4 timeout con status parcial -> el ledger refleja SOLO lo que de verdad
       salio; el resto queda `inflight`.
  F1-5 dos drivers -> el segundo `browser.lock` no se adquiere.
  F1-6 reloj atrasado 2 h (`last_any`/`last_ts` futuros) -> `clock_jump` y
       ningun grupo queda inelegible para siempre.
  F1-7 0 elegibles -> el item queda `diferido` (no `error`), `tries` intacto y
       `next_at` futuro.
  F1-8 "Bolsa de trabajo" y un grupo con "PayPal" -> quedan fuera y el motivo
       aparece en el log.

Todo el ledger/estado vive en un `tempfile`: `CXT_DIR` se fija ANTES de
importar los modulos y se reapuntan las rutas publicas de `autopost`. NUNCA se
toca `~/.cxt/{ledger.db,autoqueue.json,autopost_attempts.json,...}`.

Uso: python3 test_f1_qa.py   (exit 0 = PASS)
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# Antes de importar: cualquier `ledger.db_path()` implicita cae en el tempdir.
_TMP = tempfile.mkdtemp(prefix="f1_qa_")
os.environ["CXT_DIR"] = _TMP
_REPO_DB = os.path.join(_HERE, "ledger.db")
_REPO_DB_BEFORE = os.path.exists(_REPO_DB)

import autopost as AP  # noqa: E402
import ledger as L  # noqa: E402

# Reapunta TODAS las rutas de estado del worker al tempdir (nada real).
AP.QUEUE = os.path.join(_TMP, "autoqueue.json")
AP.ATTEMPTS = os.path.join(_TMP, "autopost_attempts.json")
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.SHARE_CMD = os.path.join(_TMP, "share_post.cmd.json")
AP.SHARE_STATUS = os.path.join(_TMP, "share_post_status.json")
AP.GROUPS = os.path.join(_TMP, "groups.json")        # inexistente: no siembra
AP.RELEVANCE = os.path.join(_TMP, "relevance.json")
AP.LOCK = os.path.join(_TMP, "browser.lock")
# F1.1: el halt, el pid y el cmd tambien son estado del worker; si no se
# reapuntan, `_on_term` borraria el pid REAL y `active_halt` leeria el halt real.
AP.HALT = os.path.join(_TMP, "autopost.halt.json")
AP.PID = os.path.join(_TMP, "autopost.pid")
AP.CMD = os.path.join(_TMP, "autopost.cmd.json")
AP.WORKER_LOCK = os.path.join(_TMP, "autopost.lock")
shutil.copy(os.path.join(_HERE, "relevance.json"), AP.RELEVANCE)

fails = []
NOW = int(time.time() * 1000)
HOUR = 3600 * 1000

# Huella del estado REAL: se compara al final para probar que la suite no lo
# toco (ni lo creo). Se captura DESPUES de importar: importar no escribe nada.
_REAL_STATE_NAMES = ("ledger.db", "autoqueue.json", "autopost_attempts.json",
                     "autopost_status.json", "browser.lock",
                     "autopost.halt.json")


def _snap(name):
    path = os.path.join(_HERE, name)
    try:
        st = os.stat(path)
        return (True, st.st_size, st.st_mtime_ns)
    except OSError:
        return (False, 0, 0)


_REAL_STATE_BEFORE = {name: _snap(name) for name in _REAL_STATE_NAMES}


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name
          + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


def _reset_default_ledger(rows):
    """Deja el ledger por defecto (temp) con SOLO las filas dadas en `grp`.

    `row` = (gid, name, status). warm_at/status_until/last_any/day_n en 0: los
    grupos quedan calientes, sin cuarentena y sin gap, salvo lo que el test
    ajuste despues.
    """
    L.init()
    conn = L.connect()
    conn.execute("DELETE FROM grp")
    conn.execute("DELETE FROM post_group")
    for gid, name, status in rows:
        conn.execute(
            "INSERT INTO grp(gid, name, status, warm_at, status_until, last_any, day_n)"
            " VALUES(?, ?, ?, 0, 0, 0, 0)", (gid, name, status))
    conn.close()


def _run_tick(queue_items, groups_rows, cmd_path):
    """Corre un `_tick` aislado (health/tab/checkpoint falseados) y captura el log.

    Devuelve (status, salida_capturada). El ledger por defecto se re-siembra con
    `groups_rows`; la cola se escribe en `AP.QUEUE`.
    """
    _reset_default_ledger(groups_rows)
    with open(AP.QUEUE, "w", encoding="utf-8") as fh:
        json.dump({"paused": False, "items": queue_items}, fh)
    st = AP.default_status()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        AP._tick(st, cmd_path)
    return st, buf.getvalue()


# Guardamos los monkeypatches de `_tick` para restaurarlos al final.
_orig_health = AP.health
_orig_dedicated = AP._dedicated_tab
_orig_checkpoint = AP.checkpoint_now
AP.health = lambda: {"enabled": True, "share_alive": False, "share_post_alive": False}
AP._dedicated_tab = lambda: None
AP.checkpoint_now = lambda tab: ""
_NO_CMD = os.path.join(_TMP, "no-cmd.json")

try:
    # ============================================================ F1-1
    print("== F1-1: ledger sembrado -> la ronda elige solo el elegible ==")
    DB1 = os.path.join(_TMP, "f1.db")
    L.init(DB1)
    L.group_state("ok1", name="Bazar Vecinos", db=DB1)            # elegible
    L.group_state("blk", name="Mercado Playa", status="blocked",
                  status_until=NOW + HOUR, db=DB1)                # cuarentena futura
    L.group_state("warm", name="Ventas Guadalupana", db=DB1)
    _c1 = L.connect(DB1)
    _c1.execute("UPDATE grp SET warm_at=? WHERE gid='warm'", (NOW + HOUR,))
    _c1.close()

    pk1 = L.post_key("https://www.facebook.com/groups/1/posts/100/")
    groups1 = L.groups_map(DB1)
    post1 = L.post_group_map(DB1)

    # Centinela en el registro de intentos: select_round NO debe tocarlo.
    AP.save_attempts({"sentinel": {"state": "hecho", "at": NOW, "tries": 1,
                                   "published": 0, "pending": 0}})
    _att_before = open(AP.ATTEMPTS, "rb").read()
    sel1 = AP.select_round(groups1, post1, pk1, NOW)
    _att_after = open(AP.ATTEMPTS, "rb").read()

    check("F1-1 elige exactamente 1 gid", sel1["gids"] == ["ok1"], sel1["gids"])
    check("F1-1 ese gid es el ok", len(sel1["gids"]) == 1 and sel1["gids"][0] == "ok1",
          sel1["gids"])
    check("F1-1 descarta blocked y warm (2 motivos)",
          len(sel1["discarded"]) == 2, sel1["discarded"])
    check("F1-1 motivo blocked", any(d["reason"] == "status:blocked" for d in sel1["discarded"]),
          sel1["discarded"])
    check("F1-1 motivo warm", any(d["reason"] == "warm" for d in sel1["discarded"]),
          sel1["discarded"])
    check("F1-1 autopost_attempts.json sin cambios", _att_before == _att_after)

    # ============================================================ F1-2
    print("== F1-2: corte a mitad de ronda -> reconciliar sin re-publicar ==")
    DB2 = os.path.join(_TMP, "f2.db")
    L.init(DB2)
    pk2 = L.post_key("https://www.facebook.com/groups/1/posts/200/")
    L.intent(AP.intent_rows(["a", "b", "c"], pk2, NOW), batch="bcut", db=DB2)
    check("F1-2 arranca con 3 inflight", len(L.inflight(db=DB2)) == 3, L.inflight(db=DB2))

    _keys_before = set(L.post_group_map(DB2).keys())
    _intent_calls = []
    _orig_intent = L.intent
    _orig_sp_run2 = AP.subprocess.run

    def _counting_intent(*a, **k):
        _intent_calls.append(1)
        return _orig_intent(*a, **k)

    def _forbid_share(*a, **k):
        raise AssertionError("reconciliar jamas debe lanzar share_post")

    L.intent = _counting_intent
    AP.subprocess.run = _forbid_share
    try:
        rec2 = AP.reconcile_ledger(evidence_fn=lambda row: None, db=DB2, use_browser=False)
    finally:
        L.intent = _orig_intent
        AP.subprocess.run = _orig_sp_run2

    _keys_after = set(L.post_group_map(DB2).keys())
    check("F1-2 reconcilia los 3 inflight", rec2["checked"] == 3, rec2)
    check("F1-2 sin evidencia -> 3 unknown", rec2["unknown"] == 3 and rec2["ok"] == 0, rec2)
    check("F1-2 no queda ningun inflight", L.inflight(db=DB2) == [], L.inflight(db=DB2))
    check("F1-2 cero nuevas intenciones (no re-publica)", _intent_calls == [], _intent_calls)
    check("F1-2 no se anaden filas nuevas al ledger", _keys_before == _keys_after,
          (_keys_before, _keys_after))
    check("F1-2 las filas resueltas quedan unknown",
          all(v["status"] == "unknown" for v in L.post_group_map(DB2).values()),
          L.post_group_map(DB2))

    # ============================================================ F1-3
    print("== F1-3: reintento con published>0 -> cubre pendientes, no repite ==")
    DB3 = os.path.join(_TMP, "f3.db")
    L.init(DB3)
    for _gid, _nm in (("d", "Bazar Vecinos"), ("e", "Mercado Playa"),
                      ("f", "Ventas Guadalupana")):
        L.group_state(_gid, name=_nm, db=DB3)
    pk3 = L.post_key("https://www.facebook.com/groups/1/posts/300/")
    # `d` y `e` ya salieron para ESTA publicacion (cooldown activo); `f` pendiente.
    post3 = {("d", pk3): {"last_ts": NOW}, ("e", pk3): {"last_ts": NOW}}
    sel3 = AP.select_round(L.groups_map(DB3), post3, pk3, NOW)
    check("F1-3 no re-publica los grupos ya hechos", "d" not in sel3["gids"] and "e" not in sel3["gids"],
          sel3["gids"])
    check("F1-3 si cubre el pendiente", sel3["gids"] == ["f"], sel3["gids"])
    check("F1-3 _retryable con published>0 es False",
          AP._retryable({"dry": False},
                        {"published": 2, "pending": 0, "tries": 1}) is False)

    # ============================================================ F1-4
    print("== F1-4: timeout con status parcial -> el ledger refleja lo real ==")
    DB4 = os.path.join(_TMP, "f4.db")
    L.init(DB4)
    pk4 = L.post_key("https://www.facebook.com/groups/1/posts/400/")
    L.intent(AP.intent_rows(["p1", "p2"], pk4, NOW), batch="bt", db=DB4)
    partial = {"running": False, "published": 1, "results": [
        {"gid": "p1", "ok": True, "status": "ok",
         "proof": "https://www.facebook.com/groups/p1/posts/900/"}]}

    def _fake_timeout(*args, **kwargs):
        with open(AP.SHARE_STATUS, "w", encoding="utf-8") as fh:
            json.dump(partial, fh)
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    _orig_sp_run4 = AP.subprocess.run
    AP.subprocess.run = _fake_timeout
    try:
        item4 = {"url": "https://www.facebook.com/groups/1/posts/400/", "dry": False,
                 "_gids": ["p1", "p2"], "_post_key": pk4, "_lock_held": True}
        raw4 = AP.run_share(item4)
        applied4 = AP.apply_round_results(["p1", "p2"], pk4, raw4, batch="bt",
                                          ts=NOW, db=DB4)
    finally:
        AP.subprocess.run = _orig_sp_run4

    pm4 = L.post_group_map(DB4)
    check("F1-4 run_share devuelve status parcial", raw4.get("partial") is True, raw4)
    check("F1-4 el timeout conserva solo lo que salio",
          [r["gid"] for r in (raw4.get("results") or [])] == ["p1"], raw4.get("results"))
    check("F1-4 el grupo que salio queda ok",
          pm4[("p1", pk4)]["status"] == "ok", pm4.get(("p1", pk4)))
    # P3: una ronda parcial CIERRA en `unknown` terminal los gids no vistos.
    # No quedan `inflight`: el siguiente arranque no los reintenta a ciegas.
    _p24 = pm4.get(("p2", pk4)) or {}
    check("F1-4 el grupo sin evidencia queda unknown terminal",
          _p24.get("status") == "unknown" and L.inflight(db=DB4) == [],
          (_p24, L.inflight(db=DB4)))
    check("F1-4 la prueba documenta el cierre parcial (timeout_parcial)",
          _p24.get("proof") == '{"reason":"timeout_parcial"}', _p24.get("proof"))
    check("F1-4 seal_partial deja la senal timeout_parcial",
          any(s.get("kind") == "timeout_parcial" for s in L.signals(db=DB4)),
          L.signals(db=DB4))
    check("F1-4 published cuenta solo el real", applied4["published"] == 1, applied4)

    # ============================================================ F1-5
    print("== F1-5: dos drivers -> el segundo no toma el lock ==")
    fh1 = L.lock_browser(AP.LOCK)
    check("F1-5 el primer driver toma browser.lock", bool(fh1))
    fh2 = L.lock_browser(AP.LOCK)
    check("F1-5 el segundo driver no lo adquiere", not fh2, fh2)
    L.unlock_browser(fh1)

    # ============================================================ F1-6
    print("== F1-6: reloj atrasado 2 h -> clock_jump, nada inelegible para siempre ==")
    DB6 = os.path.join(_TMP, "f6.db")
    L.init(DB6)
    L.group_state("gA", name="Bazar Vecinos", db=DB6)
    L.group_state("gA", last_any=NOW + 2 * HOUR, db=DB6)   # evento "futuro"
    L.group_state("gB", name="Ventas Guadalupana", db=DB6)  # sano
    L.group_state("gC", name="Mercado Playa", db=DB6)
    pk6 = L.post_key("https://www.facebook.com/groups/1/posts/600/")
    post6 = {("gA", pk6): {"last_ts": NOW + 2 * HOUR},
             ("gC", pk6): {"last_ts": NOW + 2 * HOUR}}
    sel6 = AP.select_round(L.groups_map(DB6), post6, pk6, NOW)
    check("F1-6 reporta clock_jump", sel6["clock_jump"] is True, sel6)
    check("F1-6 el grupo sano sigue elegible", sel6["gids"] == ["gB"], sel6["gids"])
    check("F1-6 ningun descartado queda inelegible para siempre",
          all(d["free_at"] <= NOW + max(AP.GAP_GRUPO, AP.COOLDOWN_POST) + 60000
              for d in sel6["discarded"]), sel6["discarded"])
    check("F1-6 el reloj atrasado no recalcula un gap de +2 h",
          all(d["free_at"] <= NOW + AP.GAP_GRUPO + 60000 for d in sel6["discarded"]),
          sel6["discarded"])

    # ============================================================ F1-7
    print("== F1-7: 0 elegibles -> diferido (no error), tries intacto ==")
    u7 = "https://www.facebook.com/groups/1/posts/700/"
    AP.save_attempts({})
    st7, _out7 = _run_tick([{"url": u7, "state": "pendiente"}],
                           [("n1", "Bolsa de trabajo Cancun", "ok")], _NO_CMD)
    check("F1-7 el item queda diferido (no error)",
          st7["items"][0]["state"] == "diferido", st7["items"])
    check("F1-7 el item no arrastra error", st7["items"][0]["error"] == "", st7["items"])
    check("F1-7 status sin error por falta de grupos", st7.get("error") == "", st7.get("error"))
    check("F1-7 next_at en el futuro",
          int(st7.get("next_at") or 0) >= int(time.time() * 1000), st7.get("next_at"))
    check("F1-7 tries intacto (sin intento escrito)",
          AP.item_key({"url": u7}) not in AP.load_attempts(), AP.load_attempts())

    # ============================================================ F1-8
    print("== F1-8: bolsa de trabajo / PayPal fuera y el motivo sale en el log ==")
    u8 = "https://www.facebook.com/groups/1/posts/800/"
    st8, out8 = _run_tick(
        [{"url": u8, "state": "pendiente"}],
        [("n1", "Bolsa de trabajo Cancun", "ok"),
         ("n2", "Compra y vende PayPal", "ok")],
        _NO_CMD)
    check("F1-8 'Bolsa de trabajo' queda fuera y se registra en el log",
          "token_negativo" in out8 and "Bolsa de trabajo" in out8, out8[-400:])
    check("F1-8 el grupo con PayPal queda fuera y se registra",
          "PayPal" in out8, out8[-400:])
    check("F1-8 sin elegibles el item queda diferido", st8["items"][0]["state"] == "diferido",
          st8["items"])

    # ============================================================ F1-9
    print("== F1-9: identidad de URL (misma clave para el mismo post) ==")
    u_photo1 = "https://www.facebook.com/photo/?fbid=123&set=a.9"
    u_photo2 = "https://www.facebook.com/photo/?SET=A&FBID=123"
    u_group = "https://www.facebook.com/groups/1/posts/123/"
    u_perma = "https://www.facebook.com/permalink.php?story_fbid=123&id=1"
    pk_photo1 = L.post_key(u_photo1)
    pk_photo2 = L.post_key(u_photo2)
    pk_group = L.post_key(u_group)
    pk_perma = L.post_key(u_perma)

    check("F1-9 photo fbid (minusculas/mayusculas) -> misma clave fbid:123",
          pk_photo1 == pk_photo2 == "fbid:123", (pk_photo1, pk_photo2))
    check("F1-9 ruta de grupo y permalink clasico -> misma clave post:1:123",
          pk_group == pk_perma == "post:1:123", (pk_group, pk_perma))
    # Diferencia INTENCIONAL y documentada: `fbid:123` identifica la foto por su
    # id y `post:1:123` identifica el post del grupo 1; son clases distintas.
    check("F1-9 fbid y post de grupo son clases distintas (no se mezclan)",
          pk_photo1 != pk_group, (pk_photo1, pk_group))
    check("F1-9 dos publicaciones distintas NO colisionan",
          L.post_key("https://www.facebook.com/groups/1/posts/999/") != pk_group,
          L.post_key("https://www.facebook.com/groups/1/posts/999/"))
    u_pl_a = "https://www.facebook.com/Some/Path/?B=2&a=1&ref=share#frag"
    u_pl_b = "https://www.facebook.com/Some/Path/?a=1&b=2"
    check("F1-9 pl: minusculas, parametros ordenados y sin tracking",
          L.post_key(u_pl_a) == L.post_key(u_pl_b)
          == "pl:https://www.facebook.com/some/path?a=1&b=2",
          (L.post_key(u_pl_a), L.post_key(u_pl_b)))
    # La clave de intentos (`permalink_key`) NO es la identidad del ledger: es
    # la clave por URL EXACTA y a proposito conserva caso/orden (diferencia
    # documentada aqui). Lo que importa es que cada URL sea determinista.
    a_photo1 = AP.permalink_key(u_photo1)
    a_photo2 = AP.permalink_key(u_photo2)
    check("F1-9 attempts conserva caso/orden: diferencia intencional con ledger",
          a_photo1 != a_photo2 and pk_photo1 == pk_photo2, (a_photo1, a_photo2))
    check("F1-9 attempts es determinista para la MISMA url",
          AP.permalink_key(u_photo1) == a_photo1
          and L.post_key(u_photo1) == pk_photo1, (a_photo1, pk_photo1))
    # P6: solo un `dry: false` explicito publica de verdad; un item sin `dry`
    # es simulacion y su clave lleva el sufijo ":dry".
    check("F1-9 item_key real (dry=False) usa permalink_key sin sufijo",
          AP.item_key({"url": u_photo1, "dry": False}) == a_photo1,
          AP.item_key({"url": u_photo1, "dry": False}))
    check("F1-9 item_key sin dry es simulacion (sufijo :dry)",
          AP.item_key({"url": u_photo1}) == a_photo1 + ":dry",
          AP.item_key({"url": u_photo1}))
    check("F1-9 is_dry: ausente=True, True=True, False=False",
          AP.is_dry({}) is True and AP.is_dry({"dry": True}) is True
          and AP.is_dry({"dry": False}) is False,
          (AP.is_dry({}), AP.is_dry({"dry": True}), AP.is_dry({"dry": False})))

    # ============================================================ F1-10
    print("== F1-10: unknown es terminal (nunca se vuelve a seleccionar) ==")
    DB10 = os.path.join(_TMP, "f10.db")
    L.init(DB10)
    L.group_state("g1", name="Bazar Vecinos", db=DB10)
    pk10 = L.post_key("https://www.facebook.com/groups/1/posts/1010/")
    L.result("g1", pk10, "unknown", 0, "", "", NOW, DB10)
    # Dia distinto a hoy: elimina el motivo `cupo_diario` y deja visible el de
    # la pareja (lo que se quiere probar es la terminalidad, no el tope).
    L.group_state("g1", day_utc="2000-01-01", day_n=0, last_any=0, db=DB10)
    for _etiqueta, _t in (("ahora", NOW), ("+6h", NOW + 6 * HOUR),
                          ("+7h", NOW + 7 * HOUR)):
        _gm = L.groups_map(DB10)
        _pm = L.post_group_map(DB10)
        _sel = AP.select_round(_gm, _pm, pk10, _t)
        check("F1-10 unknown no se re-selecciona %s" % _etiqueta,
              _sel["gids"] == [], _sel["gids"])
        check("F1-10 motivo pair:unknown %s" % _etiqueta,
              any(d["reason"] == "pair:unknown" for d in _sel["discarded"]),
              _sel["discarded"])

    u10 = "https://www.facebook.com/groups/1/posts/1011/"
    AP.save_attempts({})
    key10 = AP.item_key({"url": u10})
    AP.attempt_mark(key10, "error", "incompleto", unresolved=1)
    _a10 = AP.load_attempts().get(key10) or {}
    check("F1-10 unresolved se persiste", int(_a10.get("unresolved") or 0) == 1, _a10)
    check("F1-10 _retryable False con unresolved>0",
          AP._retryable({"dry": False}, _a10) is False, _a10)
    _it10 = {"url": u10, "state": "pendiente"}
    AP.recover_states([_it10], AP.load_attempts())
    check("F1-10 el item queda error con motivo explicito",
          _it10["state"] == "error"
          and "1 sin prueba (no se reintenta)" in _it10["error"], _it10)

    # ============================================================ F1-11
    print("== F1-11: ok respeta COOLDOWN_POST y se re-selecciona despues ==")
    DB11 = os.path.join(_TMP, "f11.db")
    L.init(DB11)
    L.group_state("g1", name="Bazar Vecinos", db=DB11)
    pk11 = L.post_key("https://www.facebook.com/groups/1/posts/1111/")
    last11 = NOW - AP.COOLDOWN_POST + 60000
    post11 = {("g1", pk11): {"status": "ok", "last_ts": last11}}
    check("F1-11 COOLDOWN_POST son 6 h", AP.COOLDOWN_POST == 6 * HOUR,
          AP.COOLDOWN_POST)
    sel11 = AP.select_round(L.groups_map(DB11), post11, pk11, NOW)
    check("F1-11 ok reciente NO se re-selecciona", sel11["gids"] == [], sel11["gids"])
    check("F1-11 motivo cooldown_post",
          any(d["reason"] == "cooldown_post" for d in sel11["discarded"]),
          sel11["discarded"])
    check("F1-11 free_at = last_ts + COOLDOWN_POST",
          any(int(d["free_at"]) == last11 + AP.COOLDOWN_POST
              for d in sel11["discarded"]), sel11["discarded"])
    sel11b = AP.select_round(L.groups_map(DB11), post11, pk11,
                             last11 + AP.COOLDOWN_POST)
    check("F1-11 ok + COOLDOWN_POST SI se re-selecciona",
          sel11b["gids"] == ["g1"], sel11b["gids"])

    # ============================================================ F1-12
    print("== F1-12: cuarentena caduca -> el grupo sirve para otra publicacion ==")
    DB12 = os.path.join(_TMP, "f12.db")
    L.init(DB12)
    L.group_state("q_past", name="Bazar Vecinos", status="blocked",
                  status_until=NOW - HOUR, db=DB12)
    L.group_state("q_future", name="Ventas Guadalupana", status="blocked",
                  status_until=NOW + HOUR, db=DB12)
    _exp12 = L.expire_quarantine(now=NOW, db=DB12)
    check("F1-12 expire_quarantine devuelve 1 fila caducada", _exp12 == 1, _exp12)
    _gm12 = L.groups_map(DB12)
    check("F1-12 blocked con status_until pasado -> ok",
          _gm12["q_past"]["status"] == "ok"
          and int(_gm12["q_past"]["status_until"]) == 0, _gm12.get("q_past"))
    check("F1-12 blocked con status_until futuro sigue bloqueado",
          _gm12["q_future"]["status"] == "blocked", _gm12.get("q_future"))
    pk12 = L.post_key("https://www.facebook.com/groups/1/posts/1212/")
    sel12 = AP.select_round(_gm12, {}, pk12, NOW)
    check("F1-12 el grupo caducado sirve para OTRA publicacion",
          sel12["gids"] == ["q_past"], sel12["gids"])
    _exp12b = L.expire_quarantine(now=NOW, db=DB12)
    check("F1-12 una cuarentena futura no caduca", _exp12b == 0, _exp12b)
    sel12b = AP.select_round(L.groups_map(DB12), {}, pk12, NOW)
    check("F1-12 el grupo futuro sigue sin ser elegible",
          "q_future" not in sel12b["gids"]
          and any(d["gid"] == "q_future" and d["reason"] == "status:blocked"
                  for d in sel12b["discarded"]),
          (sel12b["gids"], sel12b["discarded"]))

    # ============================================================ F1-13
    print("== F1-13: el tope cuenta unknown y day_n sube con cualquier estado ==")
    DB13 = os.path.join(_TMP, "f13.db")
    L.init(DB13)
    pk13 = L.post_key("https://www.facebook.com/groups/1/posts/1313/")
    for _gid in ("g_unknown", "g_unknown2", "g_ok", "g_blocked", "g_pending"):
        L.group_state(_gid, name="Bazar Vecinos", db=DB13)
    L.result("g_unknown", pk13, "unknown", 0, "", "", NOW, DB13)
    _att13 = L.attempted_today(db=DB13)
    check("F1-13 attempted_today cuenta unknown",
          int(_att13.get("unknown") or 0) >= 1
          and int(_att13.get("attempted") or 0) >= 1, _att13)
    for _gid, _status, _ok in (("g_ok", "ok", True),
                               ("g_unknown2", "unknown", False),
                               ("g_blocked", "blocked", False),
                               ("g_pending", "pending", False)):
        AP.apply_round_results(
            [_gid], pk13,
            {"results": [{"gid": _gid, "status": _status, "ok": _ok,
                          "proof": "p" if _ok else ""}]},
            ts=NOW, db=DB13)
    _gm13 = L.groups_map(DB13)
    for _gid in ("g_ok", "g_unknown2", "g_blocked", "g_pending"):
        check("F1-13 day_n=1 tras %s (cualquier resultado consume cupo)" % _gid,
              int(_gm13[_gid].get("day_n") or 0) == 1
              and str(_gm13[_gid].get("day_utc") or "") == L.utc_day(NOW),
              _gm13.get(_gid))
    _att13b = L.attempted_today(db=DB13)
    check("F1-13 attempted_today incluye los unknown aplicados",
          int(_att13b.get("unknown") or 0) >= 2
          and int(_att13b.get("attempted") or 0) >= 5, _att13b)
    sel13 = AP.select_round(_gm13, L.post_group_map(DB13), pk13, NOW)
    check("F1-13 ningun grupo con day_n=1 vuelve a salir hoy",
          sel13["gids"] == [], sel13["gids"])
    # `gap_grupo` se evalua antes que el cupo y taparia el motivo: se aisla el
    # cupo poniendo last_any=0 (el resultado ya consumio el dia).
    _gm13b = {_gid: dict(_g) for _gid, _g in _gm13.items()}
    for _gid in ("g_ok", "g_unknown2", "g_pending"):
        _gm13b[_gid]["last_any"] = 0
    sel13b = AP.select_round(_gm13b, L.post_group_map(DB13), pk13, NOW)
    check("F1-13 motivo cupo_diario (MAX_GRUPO_DIA=%d)" % AP.MAX_GRUPO_DIA,
          any(d["reason"] == "cupo_diario" for d in sel13b["discarded"]),
          sel13b["discarded"])

    # ============================================================ F1-14
    print("== F1-14: driver unico (--cmd propio) y muerte del hijo ==")
    _seen14 = {}
    _orig_run14 = AP.subprocess.run
    _orig_active14 = AP.active_tabs
    _orig_restore14 = AP.restore_tabs
    AP.active_tabs = lambda: []
    AP.restore_tabs = lambda ids: None

    def _fake_run14(cmd, **kwargs):
        _seen14["cmd"] = list(cmd)
        _seen14["path"] = cmd[cmd.index("--cmd") + 1]
        with open(_seen14["path"], encoding="utf-8") as fh:
            _seen14["cfg"] = json.load(fh)
        with open(AP.SHARE_STATUS, "w", encoding="utf-8") as fh:
            json.dump({"finished_at": int(time.time() * 1000),
                       "results": [{"gid": "h1", "ok": True, "status": "ok",
                                    "proof": "p"}]}, fh)
        return subprocess.CompletedProcess(cmd, 0)

    AP.subprocess.run = _fake_run14
    try:
        raw14 = AP.run_share({"url": "https://www.facebook.com/groups/1/posts/1414/",
                              "dry": False, "_gids": ["h1"],
                              "_post_key": "pl:qa14", "_lock_held": True,
                              "_batch": "qa14"})
    finally:
        AP.subprocess.run = _orig_run14
        AP.active_tabs = _orig_active14
        AP.restore_tabs = _orig_restore14
    _path14 = _seen14.get("path") or ""
    check("F1-14 el hijo lee un cmd propio (nunca share_post.cmd.json)",
          _path14 != AP.SHARE_CMD
          and os.path.basename(_path14).startswith("share_post.cmd.")
          and _path14.endswith(".json"), _path14)
    check("F1-14 se pasa --cmd con ESE path",
          "--cmd" in (_seen14.get("cmd") or [])
          and _seen14["cmd"][_seen14["cmd"].index("--cmd") + 1] == _path14,
          _seen14.get("cmd"))
    check("F1-14 el cmd lleva los gids pedidos",
          (_seen14.get("cfg") or {}).get("gids") == ["h1"], _seen14.get("cfg"))
    check("F1-14 el cmd de la ronda se borra al terminar",
          not os.path.exists(_path14), _path14)
    check("F1-14 run_share devuelve resultados",
          bool((raw14 or {}).get("results")), raw14)

    _proc14 = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"],
                               start_new_session=True)
    AP._CHILD = _proc14
    try:
        AP._kill_child()
    finally:
        AP._CHILD = None
    check("F1-14 _kill_child mata al hijo", _proc14.poll() is not None,
          _proc14.poll())
    if _proc14.poll() is None:
        _proc14.kill()

    _fh14 = L.lock_browser(AP.LOCK)
    check("F1-14 el driver toma browser.lock", bool(_fh14))
    _proc14b = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"],
                                start_new_session=True)
    AP._CHILD = _proc14b
    try:
        try:
            AP._on_term()
        except SystemExit:
            pass
    finally:
        AP._CHILD = None
    check("F1-14 _on_term mata al hijo", _proc14b.poll() is not None,
          _proc14b.poll())
    check("F1-14 el lock sigue tomado tras matar (se libera despues)",
          L.lock_browser(AP.LOCK) is None)
    L.unlock_browser(_fh14)
    if _proc14b.poll() is None:
        _proc14b.kill()

    # ============================================================ F1-15
    print("== F1-15: cobertura 20/20 (ronda de 10, item sigue pendiente) ==")
    _rows15 = [("v%02d" % _i, "Bazar Vecinos %d" % _i, "ok")
               for _i in range(20)]
    _reset_default_ledger(_rows15)
    u15 = "https://www.facebook.com/groups/1/posts/1515/"
    with open(AP.QUEUE, "w", encoding="utf-8") as fh:
        json.dump({"paused": False, "items": [{"url": u15,
                                               "state": "pendiente",
                                               "dry": False}]}, fh)
    AP.save_attempts({})
    _orig_share15 = AP.run_share
    AP.run_share = lambda item: {
        "published": len(item.get("_gids") or []),
        "results": [{"gid": _g, "ok": True, "status": "ok", "proof": "p"}
                    for _g in (item.get("_gids") or [])]}
    try:
        st15a = AP.default_status()
        AP._tick(st15a, _NO_CMD)
        check("F1-15 ronda 1 cubre MAXGROUPS=10",
              len(L.post_group_map()) == AP.MAXGROUPS, len(L.post_group_map()))
        check("F1-15 tras 10/20 el item NO queda hecho",
              st15a["items"][0]["state"] == "pendiente", st15a["items"])
        check("F1-15 done_today refleja 10",
              int(st15a.get("done_today") or 0) == 10, st15a.get("done_today"))
        st15a["next_at"] = 0
        st15b = AP.default_status()
        AP._tick(st15b, _NO_CMD)
        check("F1-15 ronda 2 cubre los otros 10",
              len(L.post_group_map()) == 20, len(L.post_group_map()))
        check("F1-15 al cubrir 20/20 el item queda hecho",
              st15b["items"][0]["state"] == "hecho", st15b["items"])
        check("F1-15 done_today refleja 20",
              int(st15b.get("done_today") or 0) == 20, st15b.get("done_today"))
    finally:
        AP.run_share = _orig_share15

    # ============================================================ F1-16
    print("== F1-16: parada dura bloquea _tick y --clear-halt la levanta ==")
    _reset_default_ledger([("h1", "Bazar Vecinos", "ok")])
    u16 = "https://www.facebook.com/groups/1/posts/1616/"
    with open(AP.QUEUE, "w", encoding="utf-8") as fh:
        json.dump({"paused": False, "items": [{"url": u16,
                                               "state": "pendiente"}]}, fh)
    AP.save_attempts({})
    _calls16 = []
    _orig_share16 = AP.run_share
    AP.run_share = lambda item: _calls16.append(item) or {"results": []}
    AP.write_halt("prueba F1-16", hours=1)
    try:
        check("F1-16 halt vigente", bool(AP.active_halt()), AP.active_halt())
        st16 = AP.default_status()
        AP._tick(st16, _NO_CMD)
        check("F1-16 _tick NO publica con halt", _calls16 == [], _calls16)
        check("F1-16 el item sigue pendiente",
              st16["items"][0]["state"] == "pendiente", st16["items"])
        check("F1-16 status marca pausado", st16.get("pausado") is True,
              st16.get("pausado"))
        check("F1-16 status explica la parada dura",
              "parada dura" in str(st16.get("error")), st16.get("error"))
    finally:
        AP.run_share = _orig_share16
    # Ruta CLI real: `--clear-halt` debe borrar el halt (no basta clear_halt).
    AP.main(["--clear-halt"])
    check("F1-16 --clear-halt borra autopost.halt.json",
          not os.path.exists(AP.HALT), AP.HALT)
    check("F1-16 tras --clear-halt no hay halt vigente",
          AP.active_halt() == {}, AP.active_halt())

    # ============================================================ F1-17
    print("== F1-17: identidad estable de publicacion (P1) ==")
    u_g17 = "https://www.facebook.com/groups/1/posts/400/"
    u_gp17 = "https://www.facebook.com/groups/1/permalink/400"
    u_pl17 = "https://www.facebook.com/permalink.php?story_fbid=400&id=1"
    check("F1-17 grupo/posts, grupo/permalink y permalink clasico -> post:1:400",
          L.post_key(u_g17) == L.post_key(u_gp17) == L.post_key(u_pl17)
          == "post:1:400",
          (L.post_key(u_g17), L.post_key(u_gp17), L.post_key(u_pl17)))
    u_fb17 = "https://www.facebook.com/photo.php?fbid=400&set=a.9"
    u_fb17b = "https://www.facebook.com/photo.php?fbid=400"
    check("F1-17 fbid ignora set= (misma clave fbid:400)",
          L.post_key(u_fb17) == L.post_key(u_fb17b) == "fbid:400",
          (L.post_key(u_fb17), L.post_key(u_fb17b)))
    # P1: `fbid` y `post:<gid>:<id>` son espacios de nombres distintos y NO se
    # unifican; el contrato no promete que una foto y un post de grupo colisionen.
    check("F1-17 fbid y post de grupo son clases distintas",
          L.post_key(u_fb17) != L.post_key(u_g17),
          (L.post_key(u_fb17), L.post_key(u_g17)))
    u_pf17a = "https://www.facebook.com/photo.php?fbid=pfbid0AbC"
    u_pf17b = "https://www.facebook.com/photo.php?fbid=pfbid0abc"
    check("F1-17 pfbid distingue mayusculas",
          L.post_key(u_pf17a) == "fbid:pfbid0AbC"
          and L.post_key(u_pf17b) == "fbid:pfbid0abc"
          and L.post_key(u_pf17a) != L.post_key(u_pf17b),
          (L.post_key(u_pf17a), L.post_key(u_pf17b)))
    check("F1-17 pfbid ignora set=",
          L.post_key(u_pf17a) == L.post_key(u_pf17a + "&set=a.1"),
          (L.post_key(u_pf17a), L.post_key(u_pf17a + "&set=a.1")))

    # ============================================================ F1-18
    print("== F1-18: ronda parcial -> unknown terminal y contadores P2 ==")
    DB18 = os.path.join(_TMP, "f18.db")
    L.init(DB18)
    for _gid18 in ("q1", "q2"):
        L.group_state(_gid18, name="Bazar Vecinos", db=DB18)
    pk18 = L.post_key("https://www.facebook.com/groups/1/posts/1818/")
    L.intent(AP.intent_rows(["q1", "q2"], pk18, NOW), batch="b18", db=DB18)
    # P2: el intento, no el resultado, es lo que consume el dia del grupo.
    _c18 = sqlite3.connect(DB18)
    _row18 = _c18.execute("SELECT day_n, last_any FROM grp WHERE gid='q2'").fetchone()
    _c18.close()
    check("F1-18 intent ya conto day_n/last_any del gid",
          _row18 is not None and int(_row18[0]) == 1 and int(_row18[1]) == NOW,
          _row18)
    partial18 = {"running": False, "published": 1, "partial": True, "results": [
        {"gid": "q1", "ok": True, "status": "ok", "proof": "p"}]}
    applied18 = AP.apply_round_results(["q1", "q2"], pk18, partial18,
                                       batch="b18", ts=NOW, db=DB18)
    _pm18 = L.post_group_map(DB18)
    check("F1-18 el gid no visto queda unknown terminal",
          (_pm18.get(("q2", pk18)) or {}).get("status") == "unknown"
          and L.inflight(db=DB18) == [],
          (_pm18.get(("q2", pk18)), L.inflight(db=DB18)))
    check("F1-18 q1 acredita el resultado real", applied18["published"] == 1,
          applied18)
    # P3 no toca `grp`: el sellado no vuelve a contar lo que ya conto `intent`.
    _c18b = sqlite3.connect(DB18)
    _row18b = _c18b.execute(
        "SELECT day_n, last_any, day_utc FROM grp WHERE gid='q2'").fetchone()
    _c18b.close()
    check("F1-18 el sellado no doble-cuenta el intento",
          _row18b is not None and int(_row18b[0]) == 1
          and int(_row18b[1]) == NOW
          and str(_row18b[2]) == L.utc_day(NOW), _row18b)
    # Se aisla el cupo/gap del dia (taparian el motivo) para dejar visible la
    # terminalidad de la pareja: es la propiedad que exige P3.
    L.group_state("q2", day_utc="2000-01-01", day_n=0, last_any=0, db=DB18)
    _sel18 = AP.select_round(L.groups_map(DB18), _pm18, pk18,
                             NOW + 6 * HOUR + 60000)
    check("F1-18 unknown no se re-selecciona ni a +6h01",
          "q2" not in _sel18["gids"]
          and any(d["gid"] == "q2" and d["reason"] == "pair:unknown"
                  for d in _sel18["discarded"]),
          (_sel18["gids"], _sel18["discarded"]))
    # P2: un `inflight` sin resultado tambien gasta el tope del dia.
    L.intent(AP.intent_rows(["q3"], pk18, NOW), batch="b18b", db=DB18)
    _att18 = L.attempted_today(L.utc_day(NOW), db=DB18)
    check("F1-18 attempted_today incluye el inflight",
          int(_att18.get("inflight") or 0) >= 1
          and int(_att18.get("attempted") or 0) >= 3, _att18)

    # ============================================================ F1-19
    print("== F1-19: REINTENTAR no borra intentos publicados/parciales ==")
    L.init()

    def _signals_nuevas(antes):
        despues = L.signals(limit=100000)
        return despues[:max(0, len(despues) - len(antes))]

    u19a = "https://www.facebook.com/groups/1/posts/1919/"
    k19a = AP.permalink_key(u19a)
    AP.save_attempts({})
    AP.attempt_mark(k19a, "error", "fallo", published=1)
    _sig19a = L.signals(limit=100000)
    check("F1-19 published>0 -> clear_attempt False",
          AP.clear_attempt(u19a) is False and k19a in AP.load_attempts(),
          AP.load_attempts())
    check("F1-19 rechazo por publicado deja senal retry_rechazado/ya_publicado",
          any(s.get("kind") == "retry_rechazado" and s.get("detail") == "ya_publicado"
              for s in _signals_nuevas(_sig19a)), _signals_nuevas(_sig19a))

    u19b = "https://www.facebook.com/groups/1/posts/1920/"
    k19b = AP.permalink_key(u19b)
    AP.save_attempts({})
    AP.attempt_mark(k19b, "error", "parcial", unresolved=1)
    _sig19b = L.signals(limit=100000)
    check("F1-19 unresolved>0 -> clear_attempt False",
          AP.clear_attempt(u19b) is False and k19b in AP.load_attempts(),
          AP.load_attempts())
    check("F1-19 rechazo parcial deja senal retry_rechazado/parcial_sin_confirmar",
          any(s.get("kind") == "retry_rechazado"
              and s.get("detail") == "parcial_sin_confirmar"
              for s in _signals_nuevas(_sig19b)), _signals_nuevas(_sig19b))

    u19c = "https://www.facebook.com/groups/1/posts/1921/"
    k19c = AP.permalink_key(u19c)
    AP.save_attempts({})
    AP.attempt_mark(k19c, "error", "limpio")
    check("F1-19 error limpio -> clear_attempt True y borra la clave",
          AP.clear_attempt(u19c) is True and k19c not in AP.load_attempts(),
          AP.load_attempts())

    u19d = "https://www.facebook.com/groups/1/posts/1922/"
    k19d = AP.permalink_key(u19d)
    AP.save_attempts({})
    AP.attempt_mark(k19d, "error", "fallo", published=1)
    _sig19d = L.signals(limit=100000)
    check("F1-19 force=True sobre published>0 -> True y borra la clave",
          AP.clear_attempt(u19d, force=True) is True
          and k19d not in AP.load_attempts(), AP.load_attempts())
    check("F1-19 force deja senal retry_forzado",
          any(s.get("kind") == "retry_forzado" for s in _signals_nuevas(_sig19d)),
          _signals_nuevas(_sig19d))

    # ============================================================ F1-20
    print("== F1-20: halt fail-closed y JS_WARN en ingles ==")
    with open(AP.HALT, "w", encoding="utf-8") as fh:
        fh.write("{no-json")
    check("F1-20 JSON corrupto -> halt activo (fail-closed)",
          bool(AP.active_halt()), AP.active_halt())
    AP.clear_halt()
    check("F1-20 halt corrupto borrado", AP.active_halt() == {}, AP.active_halt())
    check("F1-20 JS_WARN incluye 'temporarily blocked'",
          "temporarily blocked" in AP.JS_WARN.lower(), AP.JS_WARN)

    def _js_match(script, texto):
        # Extrae el regex literal del script y lo evalua como el navegador (/i).
        import re as _re
        m = _re.search(r"if\(/(.*?)/i\.test\(t\)\)", script, _re.S)
        return bool(m and _re.search(m.group(1), texto, _re.I))

    check("F1-20 el patron de JS_WARN matchea 'temporarily blocked'",
          _js_match(AP.JS_WARN, "Your account is temporarily blocked"),
          AP.JS_WARN)
    _reset_default_ledger([("z20", "Bazar Vecinos", "ok")])
    u20 = "https://www.facebook.com/groups/1/posts/2020/"
    with open(AP.QUEUE, "w", encoding="utf-8") as fh:
        json.dump({"paused": False, "items": [{"url": u20,
                                               "state": "pendiente",
                                               "dry": False}]}, fh)
    AP.save_attempts({})
    _calls20 = []
    _orig_share20 = AP.run_share
    _orig_cp20 = AP.checkpoint_now
    _orig_tabs20 = AP._tab_urls
    _orig_cdp20 = AP.cxtlib.cdp
    _orig_tab20 = AP._dedicated_tab
    AP.run_share = lambda item: _calls20.append(item) or {"results": []}
    AP.checkpoint_now = _orig_checkpoint
    AP._tab_urls = lambda: []
    AP._dedicated_tab = lambda: {"id": 1}
    # El cdp simulado evalua el script REAL (JS_WARN/JS_BLOCK) contra el texto
    # del dialogo: si el patron no cubriera la frase, no habria alerta.
    AP.cxtlib.cdp = lambda cxt, tab, script: _js_match(script, "temporarily blocked")
    try:
        st20 = AP.default_status()
        AP._tick(st20, _NO_CMD)
        check("F1-20 el aviso en ingles dispara la alerta",
              bool(st20.get("alerta")) and not AP.active_halt(),
              {"alerta": st20.get("alerta"), "error": st20.get("error")})
        check("F1-20 con alerta el worker no publica", _calls20 == [], _calls20)
    finally:
        AP.run_share = _orig_share20
        AP.checkpoint_now = _orig_cp20
        AP._tab_urls = _orig_tabs20
        AP.cxtlib.cdp = _orig_cdp20
        AP._dedicated_tab = _orig_tab20
        AP.clear_halt()
    check("F1-20 sin halt al final", AP.active_halt() == {}, AP.active_halt())

    # ============================================================ F1-21
    print("== F1-21: item sin dry -> simulacion, no toca el ledger ==")
    u21 = "https://www.facebook.com/groups/1/posts/2121/"
    item21 = {"url": u21, "state": "pendiente"}
    check("F1-21 public_items declara dry=True",
          AP.public_items([item21])[0]["dry"] is True, AP.public_items([item21]))
    check("F1-21 is_dry(item sin dry) es True", AP.is_dry(item21) is True, item21)
    _calls21 = []
    _orig_share21 = AP.run_share
    # El stub nunca publica: cero resultados y published=0, asi un item
    # simulado no puede cerrarse como exito real.
    AP.run_share = lambda item: _calls21.append(item) or {"published": 0,
                                                          "results": []}
    try:
        st21, _out21 = _run_tick([item21], [("s1", "Bazar Vecinos", "ok")],
                                 _NO_CMD)
        _cnt21 = L.counts()
        check("F1-21 dry ausente no escribe ninguna fila en el ledger",
              sum(int(_cnt21.get(_k) or 0)
                  for _k in ("ok", "unknown", "inflight", "blocked", "removed")) == 0,
              _cnt21)
        check("F1-21 dry ausente no queda hecho como publicacion real",
              st21["items"][0]["state"] != "hecho", st21["items"])
        check("F1-21 status reporta dry=True y published=0",
              st21["items"][0].get("dry") is True
              and int(st21["items"][0].get("published") or 0) == 0,
              st21["items"])
        check("F1-21 run_share recibe el item como simulacion",
              bool(_calls21) and AP.is_dry(_calls21[0]) is True, _calls21)
    finally:
        AP.run_share = _orig_share21

    # ============================================================ F1-22
    print("== F1-22: reconciliar un crash deja el cupo del grupo contado ==")
    DB22 = os.path.join(_TMP, "f22.db")
    L.init(DB22)
    L.group_state("r1", name="Bazar Vecinos", db=DB22)
    pk22 = L.post_key("https://www.facebook.com/groups/1/posts/2222/")
    L.intent(AP.intent_rows(["r1"], pk22, NOW), batch="b22", db=DB22)
    check("F1-22 arranca con el gid inflight", len(L.inflight(db=DB22)) == 1,
          L.inflight(db=DB22))
    _rec22 = AP.reconcile_ledger(evidence_fn=lambda row: None, db=DB22,
                                 use_browser=False)
    check("F1-22 reconcile resuelve el inflight como unknown",
          _rec22["checked"] == 1 and _rec22["unknown"] == 1, _rec22)
    _gm22 = L.groups_map(DB22)
    check("F1-22 tras el crash el grupo quedo con day_n>=1 y last_any",
          int((_gm22.get("r1") or {}).get("day_n") or 0) >= 1
          and int((_gm22.get("r1") or {}).get("last_any") or 0) > 0,
          _gm22.get("r1"))
    pk22b = L.post_key("https://www.facebook.com/groups/1/posts/2223/")
    _sel22 = AP.select_round(_gm22, L.post_group_map(DB22), pk22b, NOW)
    check("F1-22 otro post el mismo dia no elige ese gid",
          "r1" not in _sel22["gids"]
          and any(d["gid"] == "r1" for d in _sel22["discarded"]),
          (_sel22["gids"], _sel22["discarded"]))

    # ============================================================ F1-23
    print("== F1-23: pareja ya cubierta no se re-publica (exclude_gids) ==")
    DB23 = os.path.join(_TMP, "f23.db")
    L.init(DB23)
    pk23 = L.post_key("https://www.facebook.com/groups/1/posts/2323/")
    for _i in range(1, 13):
        L.group_state("g%02d" % _i, name="Bazar Vecinos %d" % _i, db=DB23)
    # g01..g10: pareja `ok` hace 7 h (> COOLDOWN_POST de 6 h).
    post23 = {}
    for _i in range(1, 11):
        post23[("g%02d" % _i, pk23)] = {"status": "ok",
                                       "last_ts": NOW - 7 * HOUR,
                                       "n": 1, "ok": 1, "proof": "p",
                                       "batch": ""}
    # g11 queda en calentamiento (inelegible); g12 no tiene pareja (sin cubrir).
    _c23 = L.connect(DB23)
    _c23.execute("UPDATE grp SET warm_at=? WHERE gid='g11'", (NOW + HOUR,))
    _c23.close()

    _gm23 = L.groups_map(DB23)
    _cov23 = AP.coverage_state(_gm23, post23, pk23)
    _covered23 = list(_cov23["covered_gids"])
    check("F1-23 cobertura detecta SOLO las 10 parejas ok",
          len(_covered23) == 10 and "g11" not in _covered23
          and "g12" not in _covered23, _cov23)

    # Copia superficial: los valores son dicts de escalares; sirve para probar
    # que select_round no muta ni el estado de grupos ni el de parejas.
    _gm23_before = {_k: dict(_v) for _k, _v in _gm23.items()}
    _pm23_before = {_k: dict(_v) for _k, _v in post23.items()}
    _excl23 = list(_covered23)
    sel23 = AP.select_round(_gm23, post23, pk23, NOW, maxgroups=10,
                            exclude_gids=_excl23)
    check("F1-23 con exclude_gids solo sale el gid sin cubrir",
          sel23["gids"] == ["g12"], sel23["gids"])
    check("F1-23 las 10 parejas cubiertas descartan con pair:covered",
          len([d for d in sel23["discarded"]
               if d["reason"] == "pair:covered"]) == 10, sel23["discarded"])
    check("F1-23 ningun gid excluido aparece en la ronda",
          not (set(_covered23) & set(sel23["gids"])), sel23["gids"])
    check("F1-23 select_round no muta groups_map/post_map",
          _gm23 == _gm23_before and post23 == _pm23_before,
          (sel23["gids"], sel23["discarded"]))
    check("F1-23 select_round no muta la lista exclude_gids",
          _excl23 == _covered23, _excl23)

    # §3.1 (protegido por F1-11): SIN `exclude_gids` los `ok` de hace 7 h
    # vuelven a ser elegibles. Se sube el tope para que quepan todos.
    sel23b = AP.select_round(_gm23, post23, pk23, NOW, maxgroups=20)
    check("F1-23 sin exclude_gids los ok vuelven a ser elegibles (§3.1)",
          "g12" in sel23b["gids"]
          and all(_g in sel23b["gids"] for _g in _covered23), sel23b["gids"])

    # ============================================================ F1-24
    print("== F1-24: ronda terminada sin results sella unknown (sin inflight) ==")
    DB24 = os.path.join(_TMP, "f24.db")
    L.init(DB24)
    pk24 = L.post_key("https://www.facebook.com/groups/1/posts/2424/")
    for _gid24 in ("u1", "u2"):
        L.group_state(_gid24, name="Bazar Vecinos", db=DB24)
    L.intent(AP.intent_rows(["u1", "u2"], pk24, NOW), batch="b24", db=DB24)
    check("F1-24 arranca con 2 inflight",
          len(L.inflight(batch="b24", db=DB24)) == 2,
          L.inflight(batch="b24", db=DB24))

    # El hijo murio sin dejar estado: la ronda TERMINO (no es parcial) pero no
    # hay `results`. Debe sellarse unknown, nunca quedar inflight.
    raw24 = {"ok": False, "error": "share_post no dejo estado"}
    applied24 = AP.apply_round_results(["u1", "u2"], pk24, raw24,
                                       batch="b24", ts=NOW, db=DB24)
    check("F1-24 la ronda sin results no deja inflight",
          L.inflight(batch="b24", db=DB24) == [],
          L.inflight(batch="b24", db=DB24))
    _pm24 = L.post_group_map(DB24)
    check("F1-24 las 2 filas quedan unknown",
          all((_pm24.get((_g, pk24)) or {}).get("status") == "unknown"
              for _g in ("u1", "u2")), _pm24)
    check("F1-24 unresolved contiene ambos gids",
          sorted(applied24.get("unresolved") or []) == ["u1", "u2"], applied24)
    # Se aisla cupo/gap del dia para que el motivo visible sea la terminalidad.
    for _gid24 in ("u1", "u2"):
        L.group_state(_gid24, day_utc="2000-01-01", day_n=0, last_any=0,
                      db=DB24)
    _sel24 = AP.select_round(L.groups_map(DB24), _pm24, pk24, NOW + 7 * HOUR)
    check("F1-24 unknown sigue bloqueando la re-seleccion a +7h",
          _sel24["gids"] == []
          and all(any(d["gid"] == _g and d["reason"] == "pair:unknown"
                      for d in _sel24["discarded"])
                  for _g in ("u1", "u2")),
          (_sel24["gids"], _sel24["discarded"]))

    # ============================================================ F1-25
    print("== F1-25: intent es el unico contador de cupo (sin doble conteo) ==")
    DB25 = os.path.join(_TMP, "f25.db")
    L.init(DB25)
    pk25 = L.post_key("https://www.facebook.com/groups/1/posts/2525/")
    L.group_state("c1", name="Bazar Vecinos", db=DB25)
    L.intent(AP.intent_rows(["c1"], pk25, NOW), batch="b25", db=DB25)
    _gm25 = L.groups_map(DB25)
    _last25 = int((_gm25.get("c1") or {}).get("last_any") or 0)
    check("F1-25 intent deja day_n=1 y last_any",
          int((_gm25.get("c1") or {}).get("day_n") or 0) == 1
          and _last25 == NOW, _gm25.get("c1"))

    applied25 = AP.apply_round_results(
        ["c1"], pk25,
        {"results": [{"gid": "c1", "ok": True, "status": "ok", "proof": "p"}]},
        batch="b25", ts=NOW, db=DB25)
    _gm25b = L.groups_map(DB25)
    check("F1-25 el resultado ok no vuelve a contar el mismo intento",
          int((_gm25b.get("c1") or {}).get("day_n") or 0) == 1,
          _gm25b.get("c1"))
    check("F1-25 last_any no retrocede con el resultado",
          int((_gm25b.get("c1") or {}).get("last_any") or 0) >= _last25,
          _gm25b.get("c1"))
    check("F1-25 el ok se acredita una sola vez",
          applied25["published"] == 1, applied25)

    # Sin `intent` previo (fila creada por otra version), el resultado debe
    # garantizar day_n>=1 de forma idempotente; nunca dejarlo en 0.
    L.group_state("c2", name="Bazar Vecinos", db=DB25)
    AP.apply_round_results(
        ["c2"], pk25,
        {"results": [{"gid": "c2", "ok": True, "status": "ok", "proof": "p"}]},
        batch="b25b", ts=NOW, db=DB25)
    _gm25c = L.groups_map(DB25)
    check("F1-25 sin intent previo el resultado deja day_n>=1 (no 0)",
          int((_gm25c.get("c2") or {}).get("day_n") or 0) >= 1
          and str((_gm25c.get("c2") or {}).get("day_utc") or "")
          == L.utc_day(NOW), _gm25c.get("c2"))

    # ============================================================ aislamiento
    print("== aislamiento del estado real ==")
    check("F1-QA no creo ~/.cxt/ledger.db",
          os.path.exists(_REPO_DB) == _REPO_DB_BEFORE, _REPO_DB)
    check("F1-QA uso el ledger del tempdir",
          os.path.exists(os.path.join(_TMP, "ledger.db")),
          os.path.join(_TMP, "ledger.db"))
    for _name, _before in _REAL_STATE_BEFORE.items():
        _after = _snap(_name)
        check("F1-QA no creo ni toco ~/.cxt/%s" % _name,
              _after == _before, (_before, _after))
finally:
    AP.health = _orig_health
    AP._dedicated_tab = _orig_dedicated
    AP.checkpoint_now = _orig_checkpoint
    shutil.rmtree(_TMP, ignore_errors=True)

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
if not fails:
    print("PASS")
sys.exit(0 if not fails else 1)
