#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon) de autopost.py.

Cubre la logica pura del worker (sin efectos secundarios, importable):
  - constantes de intervalo por entorno (MIN/REC/MAX, MAXGROUPS, MAXDAY)
  - clamp del intervalo
  - jitter dentro del +-30 %
  - primera publicacion solo tras el intervalo recomendado (REC)
  - tope diario (MAXDAY) y tope de grupos por publicacion (MAXGROUPS)
  - dedupe por permalink
  - tope de profundidad de cola (200)
  - caducidad de items a los 7 dias
  - resume / a-lo-mas-una-vez ("publicando" al reiniciar -> hecho)
  - pausa por checkpoint (/checkpoint -> pausado + "cuenta restringida")

Uso: python3 tests/test_autopost_unit.py   (exit 0 = PASS)
"""
import hashlib
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "scripts")):
    if os.path.exists(os.path.join(_p, "autopost.py")):
        sys.path.insert(0, _p)
        break

# F1.1: CXT_DIR ANTES de importar. Sin esto `ledger.db_path()` apuntaria al
# estado real; la suite debe ser tan hermética como test_f1_qa.py.
_TMP = tempfile.mkdtemp(prefix="autopost_unit_")
os.environ["CXT_DIR"] = _TMP

import autopost as AP  # noqa: E402
import ledger as L  # noqa: E402

# Aislamos TODAS las rutas que toca el worker para no leer/escribir en ~/.cxt.
AP.QUEUE = os.path.join(_TMP, "autoqueue.json")
AP.ATTEMPTS = os.path.join(_TMP, "autopost_attempts.json")
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.SHARE_CMD = os.path.join(_TMP, "share_post.cmd.json")
AP.SHARE_STATUS = os.path.join(_TMP, "share_post_status.json")
AP.GROUPS = os.path.join(_TMP, "groups.json")
AP.RELEVANCE = os.path.join(_TMP, "relevance.json")
AP.LOCK = os.path.join(_TMP, "browser.lock")
AP.HALT = os.path.join(_TMP, "autopost.halt.json")
AP.PID = os.path.join(_TMP, "autopost.pid")
AP.CMD = os.path.join(_TMP, "autopost.cmd.json")
AP.WORKER_LOCK = os.path.join(_TMP, "autopost.lock")

_REAL_STATE_NAMES = ("ledger.db", "autoqueue.json", "autopost_attempts.json",
                     "autopost_status.json", "browser.lock",
                     "autopost.halt.json")


def _snap(name):
    path = os.path.join(_HERE, name)
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        return (True, st.st_size, digest)
    except OSError:
        return (False, 0, "")


_REAL_STATE_BEFORE = {name: _snap(name) for name in _REAL_STATE_NAMES}

fails = []
DAY = 24 * 3600 * 1000


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


# 1. constantes por entorno ---------------------------------------------
check("CXT_AUTO_MIN=10", AP.INTERVAL_MIN == 10, AP.INTERVAL_MIN)
check("CXT_AUTO_REC=15", AP.INTERVAL_REC == 15, AP.INTERVAL_REC)
check("CXT_AUTO_MAX=60", AP.INTERVAL_MAX == 60, AP.INTERVAL_MAX)
check("CXT_AUTO_MAXGROUPS=10", AP.MAXGROUPS == 10, AP.MAXGROUPS)
check("CXT_AUTO_MAXDAY=60", AP.MAXDAY == 60, AP.MAXDAY)
check("DEPTH=200", AP.DEPTH == 200, AP.DEPTH)

# 2. clamp del intervalo ------------------------------------------------
check("clamp(1) == MIN", AP.clamp_interval(1) == AP.INTERVAL_MIN, AP.clamp_interval(1))
check("clamp(999) == MAX", AP.clamp_interval(999) == AP.INTERVAL_MAX, AP.clamp_interval(999))
check("clamp(25) == 25 (dentro)", AP.clamp_interval(25) == 25, AP.clamp_interval(25))
check("clamp invalido cae a REC", AP.clamp_interval("x") == AP.INTERVAL_REC, AP.clamp_interval("x"))

# 3. jitter dentro de +-30 % --------------------------------------------
base = AP.INTERVAL_REC * 60000
lo, hi = int(base * 0.70), int(base * 1.30)
vals = [AP.jitter_ms(base) for _ in range(500)]
check("jitter_ms nunca fuera de +-30 %", all(lo <= v <= hi for v in vals), (min(vals), max(vals)))
check("jitter_ms produce valores distintos (no patron fijo)",
      len(set(vals)) > 5, len(set(vals)))
iv = [AP.interval_ms(AP.INTERVAL_MAX) for _ in range(500)]
check("interval_ms(MAX) dentro de +-30 % del maximo",
      all(int(AP.INTERVAL_MAX * 60000 * 0.70) <= v <= int(AP.INTERVAL_MAX * 60000 * 1.30) for v in iv),
      (min(iv), max(iv)))

# 3b. el retardo EFECTIVO nunca abandona la banda dura [MIN, MAX] (IS5):
#     el clamp va DESPUES del jitter, asi interval_ms(MAX) no supera MAX ni
#     interval_ms(MIN) baja de MIN, y un valor medio (15) conserva el +-30 %.
import random as _random
_hard_lo = int(AP.INTERVAL_MIN * 60000)
_hard_hi = int(AP.INTERVAL_MAX * 60000)
_rng = _random.Random(20260101)
_v60 = [AP.interval_ms(60, _rng) for _ in range(2000)]
check("interval_ms(60) nunca supera INTERVAL_MAX*60000",
      all(v <= _hard_hi for v in _v60), (min(_v60), max(_v60)))
_v10 = [AP.interval_ms(10, _rng) for _ in range(2000)]
check("interval_ms(10) nunca baja de INTERVAL_MIN*60000",
      all(v >= _hard_lo for v in _v10), (min(_v10), max(_v10)))
_v15 = [AP.interval_ms(15, _rng) for _ in range(2000)]
check("interval_ms(15) en banda [MIN, MAX] y con jitter +-30 %",
      all(_hard_lo <= v <= _hard_hi for v in _v15)
      and all(int(AP.INTERVAL_REC * 60000 * 0.70) <= v <= int(AP.INTERVAL_REC * 60000 * 1.30)
              for v in _v15)
      and len(set(_v15)) > 5, (min(_v15), max(_v15)))

# 4. primera publicacion solo tras REC ----------------------------------
now = 1_000_000_000_000
check("first_next_at = now + REC", AP.first_next_at(now) - now == int(AP.INTERVAL_REC * 60000),
      AP.first_next_at(now) - now)
check("first_next_at >= REC siempre", AP.first_next_at(0) >= int(AP.INTERVAL_REC * 60000),
      AP.first_next_at(0))
check("next_at_for >= 0.7*intervalo", AP.next_at_for(now, 30) - now >= int(30 * 60000 * 0.7),
      AP.next_at_for(now, 30) - now)

# 5. tope diario (MAXDAY) ------------------------------------------------
check("under_daily_cap(MAXDAY-1) es True", AP.under_daily_cap(AP.MAXDAY - 1) is True)
check("under_daily_cap(MAXDAY) es False", AP.under_daily_cap(AP.MAXDAY) is False)
st = AP.default_status()
AP.roll_day(st, "2026-01-01")
check("roll_day fija el dia", st.get("day") == "2026-01-01" and st.get("done_today") == 0, st)
st["done_today"] = 5
AP.roll_day(st, "2026-01-02")
check("roll_day reinicia done_today en dia nuevo", st.get("done_today") == 0, st)
st["done_today"] = 3
AP.roll_day(st, "2026-01-02")
check("roll_day conserva done_today el mismo dia", st.get("done_today") == 3, st)

# 6. tope de grupos por publicacion (MAXGROUPS) --------------------------
check("group_limit(0) == MAXGROUPS", AP.group_limit(0) == AP.MAXGROUPS, AP.group_limit(0))
check("group_limit(3) == 3", AP.group_limit(3) == 3, AP.group_limit(3))
check("group_limit(999) == MAXGROUPS", AP.group_limit(999) == AP.MAXGROUPS, AP.group_limit(999))
check("group_limit(None) == MAXGROUPS", AP.group_limit(None) == AP.MAXGROUPS, AP.group_limit(None))

# 7. dedupe por permalink ------------------------------------------------
items = [
    {"url": "https://www.facebook.com/groups/1/posts/10/"},
    {"url": "https://www.facebook.com/groups/2/posts/20/?ref=share"},
    {"url": "https://www.facebook.com/groups/2/posts/20/"},
    {"url": "https://www.facebook.com/groups/3/posts/30/"},
]
ded = AP.dedupe_items(items)
check("dedupe: de 4 con un duplicado (query ignorada) quedan 3", len(ded) == 3, len(ded))
check("dedupe conserva el primero",
      ded[1].get("url") == items[1].get("url"), ded[1].get("url"))
blank = AP.dedupe_items([{"url": ""}, {"url": ""}])
check("dedupe no colapsa urls vacias", len(blank) == 2, len(blank))

# 8. tope de profundidad de cola (200) -----------------------------------
big = [{"url": "u%d" % i} for i in range(300)]
check("trim_depth recorta a 200", len(AP.trim_depth(big)) == AP.DEPTH, len(AP.trim_depth(big)))
check("queue_accepts(199) es True", AP.queue_accepts(list(range(199))) is True)
check("queue_accepts(200) es False (rechaza)", AP.queue_accepts(list(range(200))) is False)

# 9. caducidad de items (90 dias; el jefe subio el TTL desde 7) -----------
#    Con 7 d un ciclo de cobertura largo expiraba a mitad del primer barrido.
now_ms = 10_000_000_000_000
check("item de 1 dia NO caduca", AP.is_expired({"at": now_ms - 1 * DAY}, now_ms) is False)
check("item de 6 dias NO caduca", AP.is_expired({"at": now_ms - 6 * DAY}, now_ms) is False)
check("item de 8 dias NO caduca (TTL 90 d)", AP.is_expired({"at": now_ms - 8 * DAY}, now_ms) is False)
check("item de 91 dias SI caduca", AP.is_expired({"at": now_ms - 91 * DAY}, now_ms) is True)
check("item sin 'at' no caduca", AP.is_expired({}, now_ms) is False)

# 10. resume / a-lo-mas-una-vez ------------------------------------------
q = [{"state": "publicando"}, {"state": "pendiente"},
     {"state": "hecho"}, {"state": "error"}]
AP.recover_states(q)
check("publicando al reiniciar -> hecho", q[0]["state"] == "hecho", q[0])
check("pendiente se rearma (sigue pendiente)", q[1]["state"] == "pendiente", q[1])
check("hecho se conserva", q[2]["state"] == "hecho", q[2])
check("error se conserva (no se reintenta solo)", q[3]["state"] == "error", q[3])
check("nunca re-comparte un publicando (state != publicando)", all(i["state"] != "publicando" for i in q))
check("count_pending cuenta solo pendiente", AP.count_pending(q) == 1, AP.count_pending(q))

# 11. pausa por checkpoint -----------------------------------------------
check("is_checkpoint_url(/checkpoint)", AP.is_checkpoint_url("https://www.facebook.com/checkpoint/?next") is True)
check("is_checkpoint_url(/recover)", AP.is_checkpoint_url("https://www.facebook.com/recover/initiate") is True)
check("is_checkpoint_url(grupo normal) es False",
      AP.is_checkpoint_url("https://www.facebook.com/groups/123") is False)
cst = AP.default_status()
hit = AP.apply_checkpoint(cst, "https://www.facebook.com/checkpoint/12345")
check("apply_checkpoint devuelve True ante /checkpoint", hit is True)
check("checkpoint -> NO pausa", cst.get("pausado") is False, cst)
check("checkpoint -> alerta visible",
      cst.get("error", "").startswith("ALERTA:") and isinstance(cst.get("alerta"), dict), cst)
ok_st = AP.default_status()
check("apply_checkpoint False en url normal",
      AP.apply_checkpoint(ok_st, "https://www.facebook.com/groups/1") is False)
check("url normal no pausa", ok_st.get("pausado") is False, ok_st)

_tmpd = tempfile.mkdtemp(prefix="autopost_cmd_")
check("consume_cmd sin archivo -> {}", AP.consume_cmd(os.path.join(_tmpd, "nope.json")) == {})
_cmd_path = os.path.join(_tmpd, "autopost.cmd.json")
with open(_cmd_path, "w", encoding="utf-8") as _f:
    _f.write('{"action":"pause","value":true}')
_cmd_obj = AP.consume_cmd(_cmd_path)
check("consume_cmd lee el JSON",
      _cmd_obj.get("action") == "pause" and _cmd_obj.get("value") is True, _cmd_obj)
check("consume_cmd borra el archivo tras consumir", not os.path.exists(_cmd_path), _cmd_path)
_stop_path = os.path.join(_tmpd, "stop.cmd.json")
with open(_stop_path, "w", encoding="utf-8") as _f:
    _f.write('{"stop":true}')
check("consume_cmd reconoce stop", AP.consume_cmd(_stop_path).get("stop") is True)

# 12. el worker NUNCA escribe la cola (el daemon es su unico escritor) ----
check("no existe el simbolo write_queue", not hasattr(AP, "write_queue"))
_src = open(AP.__file__, encoding="utf-8").read()
check("autopost.py no escribe autoqueue.json",
      "write_json_atomic(QUEUE" not in _src and "write_queue" not in _src)

# 13. registro de intentos del worker (a-lo-mas-una-vez) ------------------
import json as _json
AP.save_attempts({})
_u = "https://www.facebook.com/groups/9/posts/90/"
AP.attempt_mark(AP.item_key({"url": _u}), "publicando")
_att = _json.load(open(AP.ATTEMPTS, encoding="utf-8"))
check("attempts guarda 'publicando' antes del share",
      _att[AP.item_key({"url": _u})]["state"] == "publicando", _att)
_it = {"url": _u, "state": "pendiente"}
AP.recover_states([_it], AP.load_attempts())
check("item marcado publicando -> hecho al reiniciar (no re-share)", _it["state"] == "hecho", _it)
# 13c. `recover_states` rearma un item fallido mientras le queden reintentos
#      (tries < MAX_RETRIES, sin nada publicado): estado 'pendiente' y sin motivo.
AP.attempt_mark(AP.item_key({"url": _u}, dry=False), "error", "boom")
_it2 = {"url": _u, "state": "pendiente", "dry": False}
AP.recover_states([_it2], AP.load_attempts())
check("item error con tries<MAX_RETRIES -> pendiente sin motivo",
      _it2["state"] == "pendiente" and _it2["error"] == "", _it2)
#      Agotados los reintentos (tries >= MAX_RETRIES) SI se conserva en 'error'
#      con su motivo: no se reintenta solo.
_key_u = AP.item_key({"url": _u}, dry=False)
AP.save_attempts({_key_u: {"state": "error", "error": "boom",
                           "at": int(time.time() * 1000),
                           "tries": AP.MAX_RETRIES, "published": 0, "pending": 0}})
_it2b = {"url": _u, "state": "pendiente", "dry": False}
AP.recover_states([_it2b], AP.load_attempts())
check("item error agotado (tries>=MAX_RETRIES) -> error con su motivo",
      _it2b["state"] == "error" and _it2b["error"] == "boom", _it2b)
AP.attempt_mark(AP.item_key({"url": _u}), "hecho")
_it3 = {"url": _u, "state": "pendiente"}
AP.recover_states([_it3], AP.load_attempts())
check("item hecho en attempts -> hecho (no re-share)", _it3["state"] == "hecho", _it3)
_it4 = {"url": "https://www.facebook.com/groups/8/posts/80/", "state": "pendiente"}
AP.recover_states([_it4], AP.load_attempts())
check("item no intentado sigue pendiente", _it4["state"] == "pendiente", _it4)
check("item_key usa el permalink normalizado",
      AP.item_key({"url": _u + "?ref=share"}, dry=False) == AP.permalink_key(_u),
      AP.item_key({"url": _u + "?ref=share"}, dry=False))

# 13b. read_queue nunca modifica la cola y aplica el registro de intentos --
with open(AP.QUEUE, "w", encoding="utf-8") as _f:
    _json.dump({"paused": False, "items": [{"url": _u, "state": "pendiente"}]}, _f)
_before = open(AP.QUEUE, encoding="utf-8").read()
_rq = AP.read_queue()
_after = open(AP.QUEUE, encoding="utf-8").read()
check("read_queue marca el item ya intentado como hecho",
      _rq["items"][0]["state"] == "hecho", _rq["items"][0])
check("read_queue NO modifica autoqueue.json en disco", _before == _after)

# 14. reinicio reprograma next_at >= ahora+REC (RE4) ---------------------
_now2 = 5_000_000_000_000
check("startup_next_at con next_at vencido -> ahora+REC",
      AP.startup_next_at(_now2, _now2 - 1) == _now2 + int(AP.INTERVAL_REC * 60000),
      AP.startup_next_at(_now2, _now2 - 1))
check("startup_next_at arranca esperando al menos REC",
      AP.startup_next_at(_now2, 0) - _now2 >= int(AP.INTERVAL_REC * 60000),
      AP.startup_next_at(_now2, 0))
check("startup_next_at respeta un next_at futuro",
      AP.startup_next_at(_now2, _now2 + 123456) == _now2 + 123456)

# 15. esperando:<job> fija 'current' -------------------------------------
_ws = AP.default_status()
AP.set_waiting(_ws, "share_post")
check("set_waiting fija current='esperando:share_post'",
      _ws.get("current") == "esperando:share_post", _ws.get("current"))
AP.clear_waiting(_ws)
check("clear_waiting limpia el esperando", _ws.get("current") is None, _ws.get("current"))

# 16. item sin url ni expect es invalido (no comparte el post visible) ----
check("bad_item: sin url ni expect -> malo", AP.bad_item({"text": "hola"}) != "")
check("bad_item: solo text no basta", AP.bad_item({"text": "x"}) == "item sin url ni expect",
      AP.bad_item({"text": "x"}))
check("bad_item: con url -> ok", AP.bad_item({"url": "u"}) == "")
check("bad_item: con expect -> ok", AP.bad_item({"expect": "e"}) == "")
check("bad_item: vacio -> malo", AP.bad_item({}) != "")

# 17. un dry-run NO consume el tope diario; una publicacion real SI ----------
#     (el worker debe contar en done_today solo exitos reales; un dry exitoso
#      sigue apareciendo en results pero no gasta el cupo anti-ban)
_orig_run_share = AP.run_share
_orig_status = AP.STATUS
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.run_share = lambda item: {"results": [{"ok": True}]}


def _publish_fresh(item):
    _st = AP.default_status()
    AP.roll_day(_st, "2026-01-01")
    AP._publish(_st, {}, [], item, 1_000_000_000_000)
    return _st


_dry = _publish_fresh({"url": "https://www.facebook.com/groups/1/posts/1/",
                       "dry": True, "state": "pendiente"})
check("dry ok: aparece en results", len(_dry.get("results") or []) == 1 and _dry["results"][0]["ok"] is True,
      _dry.get("results"))
check("dry ok: item queda hecho", _dry["results"][0]["state"] == "hecho", _dry["results"][0])
check("dry ok: NO incrementa done_today", _dry.get("done_today") == 0, _dry.get("done_today"))

_cap_st = AP.default_status()
AP.roll_day(_cap_st, "2026-01-01")
for _i in range(AP.MAXDAY):
    AP._publish(_cap_st, {}, [],
                {"url": "https://www.facebook.com/groups/1/posts/d%d/" % _i,
                 "dry": True, "state": "pendiente"}, 1_000_000_000_000)
check("MAXDAY dry: done_today sigue 0", _cap_st.get("done_today") == 0, _cap_st.get("done_today"))
check("MAXDAY dry: tope diario NO consumido", AP.under_daily_cap(_cap_st.get("done_today", 0)) is True,
      _cap_st.get("done_today"))

_real = _publish_fresh({"url": "https://www.facebook.com/groups/1/posts/2/",
                        "dry": False, "state": "pendiente"})
check("real ok: SI incrementa done_today", _real.get("done_today") == 1, _real.get("done_today"))

# F1.1 §3.3: un fallo con resultado `unknown` SI consume tope (antes se
# esperaba 0); se usa un gid real para que la fila quede en el ledger.
L.init()
L.group_state("g17fail", name="Bazar Vecinos")
_u17fail = "https://www.facebook.com/groups/1/posts/3/"
_pk17fail = L.post_key(_u17fail)
AP.run_share = lambda item: {"published": 0, "results": [
    {"gid": "g17fail", "ok": False, "status": "unknown", "proof": ""}]}
_now17fail = int(time.time() * 1000)
_fail = AP.default_status()
AP.roll_day(_fail, L.utc_day(_now17fail))
AP._publish(_fail, {}, [], {"url": _u17fail, "dry": False, "state": "pendiente"},
            _now17fail, gids=["g17fail"], post_key=_pk17fail)
check("fallo real (unknown): SI incrementa done_today (F1.1)",
      _fail.get("done_today") == 1, _fail.get("done_today"))

AP.run_share = _orig_run_share
AP.STATUS = _orig_status

# 18. un dry-run NO contamina el registro de intentos del worker ------------
#     Un dry NO debe marcar a-lo-mas-una-vez: si lo hiciera, un share REAL
#     posterior del MISMO permalink quedaria silenciosamente saltado.
_orig_run_share18 = AP.run_share
_orig_status18 = AP.STATUS
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.save_attempts({})
AP.run_share = lambda item: {"results": [{"ok": True}]}

_u18 = "https://www.facebook.com/groups/77/posts/770/"
_key18 = AP.permalink_key(_u18)

# 18.1 dry exitoso: en results como hecho, sin tope diario, sin intento
_stdry = AP.default_status()
AP.roll_day(_stdry, "2026-01-01")
AP._publish(_stdry, {}, [], {"url": _u18, "dry": True, "state": "pendiente"},
            1_000_000_000_000)
check("18 dry ok: aparece en results como hecho",
      len(_stdry.get("results") or []) == 1 and _stdry["results"][0]["state"] == "hecho",
      _stdry.get("results"))
check("18 dry ok: NO incrementa done_today", _stdry.get("done_today") == 0, _stdry.get("done_today"))
_att18 = AP.load_attempts()
check("18 dry ok: el permalink NO queda 'hecho' en el registro de intentos",
      (_att18.get(_key18) or {}).get("state") != "hecho", _att18)
check("18 dry ok: el permalink ni siquiera aparece en intentos",
      _key18 not in _att18, _att18)

# 18.2 el MISMO permalink encolado para REAL: la cola NO lo salta
with open(AP.QUEUE, "w", encoding="utf-8") as _f:
    _json.dump({"paused": False,
                "items": [{"url": _u18, "dry": False, "state": "pendiente"}]}, _f)
_q18 = AP.read_queue()
_it18 = AP.pick_pending(_q18["items"], 2_000_000_000_000)
check("18 real: pick_pending devuelve el item tras un dry del mismo permalink",
      _it18 is not None and _it18.get("url") == _u18, _q18["items"])

# 18.3 publicacion REAL: done_today sube y el intento queda 'hecho'
_streal = AP.default_status()
AP.roll_day(_streal, "2026-01-01")
AP._publish(_streal, {}, [], _it18, 2_000_000_000_000)
check("18 real ok: incrementa done_today", _streal.get("done_today") == 1, _streal.get("done_today"))
check("18 real ok: el intento del permalink queda 'hecho'",
      (AP.load_attempts().get(_key18) or {}).get("state") == "hecho", AP.load_attempts())

# 18.4 un SEGUNDO run real del mismo permalink se salta (a-lo-mas-una-vez)
with open(AP.QUEUE, "w", encoding="utf-8") as _f:
    _json.dump({"paused": False,
                "items": [{"url": _u18, "dry": False, "state": "pendiente"}]}, _f)
_q18b = AP.read_queue()
check("18 segundo real: el permalink ya 'hecho' no se vuelve a elegir",
      AP.pick_pending(_q18b["items"], 2_000_000_000_000) is None, _q18b["items"])

AP.run_share = _orig_run_share18
AP.STATUS = _orig_status18

# 19. _tick sincroniza st["pausado"] desde q["paused"] (pausa operativa) ------
#     El popup escribe q.paused via /autopost; el worker debe reflejarlo en cada
#     tick (antes del gate), no solo al arrancar.
_orig_health = AP.health
_orig_read_queue = AP.read_queue
_orig_dedicated = AP._dedicated_tab
_orig_checkpoint = AP.checkpoint_now
_orig_status19 = AP.STATUS
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.health = lambda: {"enabled": True, "share_alive": False, "share_post_alive": False}
AP._dedicated_tab = lambda: None
AP.checkpoint_now = lambda tab: ""
_no_cmd = os.path.join(_TMP, "no-cmd.json")

_st_pause = AP.default_status()
AP.read_queue = lambda: {"paused": True, "items": []}
AP._tick(_st_pause, _no_cmd)
check("_tick: q.paused=True -> st['pausado'] True",
      _st_pause.get("pausado") is True, _st_pause.get("pausado"))

_st_resume = AP.default_status()
_st_resume["pausado"] = True
AP.read_queue = lambda: {"paused": False, "items": []}
AP._tick(_st_resume, _no_cmd)
check("_tick: q.paused=False -> st['pausado'] False (REANUDAR)",
      _st_resume.get("pausado") is False, _st_resume.get("pausado"))

AP.health = _orig_health
AP.read_queue = _orig_read_queue
AP._dedicated_tab = _orig_dedicated
AP.checkpoint_now = _orig_checkpoint
AP.STATUS = _orig_status19

# 20. prune_attempts acota el registro sin romper a-lo-mas-una-vez -----------
_now20 = 100_000_000_000_000
_old = {"state": "hecho", "at": _now20 - (AP.ATTEMPTS_TTL_MS + 1000)}
_fresh = {"state": "hecho", "at": _now20 - 1000}
_pruned = AP.prune_attempts({"old": _old, "fresh": _fresh}, _now20)
check("prune: descarta 'hecho' mas viejo que el TTL", "old" not in _pruned, _pruned)
check("prune: conserva 'hecho' reciente", "fresh" in _pruned, _pruned)
check("prune: conserva 'publicando' aunque sea viejo",
      "pin" in AP.prune_attempts({"pin": {"state": "publicando", "at": 1}}, _now20),
      AP.prune_attempts({"pin": {"state": "publicando", "at": 1}}, _now20))
_orig_max = AP.ATTEMPTS_MAX
AP.ATTEMPTS_MAX = 3
_many = {"k%d" % i: {"state": "hecho", "at": _now20 - i} for i in range(10)}
_many["pin"] = {"state": "publicando", "at": 1}
_capped = AP.prune_attempts(_many, _now20)
check("prune: respeta el tope conservando 'publicando'",
      len(_capped) == 3 and "pin" in _capped, _capped)
check("prune: bajo el tope guarda los mas recientes",
      "k0" in _capped and "k9" not in _capped, sorted(_capped))
AP.ATTEMPTS_MAX = _orig_max

# 21. intervalo efectivo: item > cola top-level > REC (T23) ---------------
#     El modal envia interval_min como campo de nivel superior del POST
#     /autopost; el daemon lo funde DENTRO de cada item (mergeAutopostDefaults).
#     El worker DEBE honrar el valor del item en curso; leer solo la cola
#     top-level hacia que el intervalo efectivo fuera siempre REC (15).
check("effective_interval(item 10) == 10",
      AP.effective_interval({"interval_min": 10}, {}) == 10,
      AP.effective_interval({"interval_min": 10}, {}))
check("effective_interval(item 30) == 30",
      AP.effective_interval({"interval_min": 30}, {}) == 30,
      AP.effective_interval({"interval_min": 30}, {}))
check("effective_interval(item 60) == 60",
      AP.effective_interval({"interval_min": 60}, {}) == 60,
      AP.effective_interval({"interval_min": 60}, {}))
check("effective_interval sin intervalo en ningun sitio -> REC",
      AP.effective_interval({}, {}) == AP.INTERVAL_REC,
      AP.effective_interval({}, {}))
check("effective_interval clampea por debajo de MIN (item 1 -> 10)",
      AP.effective_interval({"interval_min": 1}, {}) == AP.INTERVAL_MIN,
      AP.effective_interval({"interval_min": 1}, {}))
check("effective_interval clampea por encima de MAX (item 999 -> 60)",
      AP.effective_interval({"interval_min": 999}, {}) == AP.INTERVAL_MAX,
      AP.effective_interval({"interval_min": 999}, {}))
check("effective_interval: la cola top-level es respaldo si el item no lo trae",
      AP.effective_interval({}, {"interval_min": 30}) == 30,
      AP.effective_interval({}, {"interval_min": 30}))
check("effective_interval: el item GANA a la cola top-level",
      AP.effective_interval({"interval_min": 60}, {"interval_min": 10}) == 60,
      AP.effective_interval({"interval_min": 60}, {"interval_min": 10}))

# 21b. _publish usa el intervalo del item (no siempre 15) y conserva la banda.
_orig_run_share21 = AP.run_share
_orig_status21 = AP.STATUS
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.run_share = lambda item: {"results": [{"ok": True}]}
_lo21 = int(AP.INTERVAL_MIN * 60000)
_hi21 = int(AP.INTERVAL_MAX * 60000)
for _pick in (10, 30, 60):
    _st21 = AP.default_status()
    AP.roll_day(_st21, "2026-01-01")
    _t21 = int(time.time() * 1000)
    AP._publish(_st21, {"interval_min": _pick},
                [], {"url": "https://www.facebook.com/groups/21/posts/%d/" % _pick,
                     "interval_min": _pick, "dry": True, "state": "pendiente"},
                _t21)
    check("_publish item interval_min=%d -> st.interval_min == %d" % (_pick, _pick),
          _st21.get("interval_min") == _pick, _st21.get("interval_min"))
    _delay = int(_st21.get("next_at", 0)) - _t21
    check("_publish item interval_min=%d -> next_at en banda [10,60] min" % _pick,
          _lo21 <= _delay <= _hi21, _delay)
    check("_publish item interval_min=%d -> next_at con el jitter del elegido" % _pick,
          int(_pick * 60000 * 0.70) <= _delay <= int(_pick * 60000 * 1.30), _delay)

# 21c. sin intervalo en ningun sitio -> _publish cae a REC (15).
_st21r = AP.default_status()
AP.roll_day(_st21r, "2026-01-01")
AP._publish(_st21r, {}, [], {"url": "https://www.facebook.com/groups/21/posts/rec/",
                             "dry": True, "state": "pendiente"}, 1_000_000_000_000)
check("_publish sin intervalo -> st.interval_min == REC",
      _st21r.get("interval_min") == AP.INTERVAL_REC, _st21r.get("interval_min"))

# 21d. la cola top-level es respaldo cuando el item no trae interval_min.
_st21q = AP.default_status()
AP.roll_day(_st21q, "2026-01-01")
AP._publish(_st21q, {"interval_min": 30}, [],
            {"url": "https://www.facebook.com/groups/21/posts/q/",
             "dry": True, "state": "pendiente"}, 1_000_000_000_000)
check("_publish cola top-level=30 (item sin interval_min) -> 30",
      _st21q.get("interval_min") == 30, _st21q.get("interval_min"))

AP.run_share = _orig_run_share21
AP.STATUS = _orig_status21

# 22. P3: una ronda PARCIAL sella los gids no vistos como `unknown` ----------
#     Terminal: los que si reportaron (aunque sea ok) quedan intactos.
L.init()
_p3_ts = int(time.time() * 1000)
_p3_batch = "bP3test"
_p3_gids = ["gP3a", "gP3b", "gP3c"]
_p3_pk = "post:1:333"
L.intent([{"gid": _g, "post_key": _p3_pk, "ts": _p3_ts} for _g in _p3_gids],
         batch=_p3_batch)
_p3 = AP.apply_round_results(
    _p3_gids, _p3_pk,
    {"partial": True, "results": [
        {"gid": "gP3a", "ok": True, "status": "ok", "proof": "https://x/prueba"}]},
    batch=_p3_batch, ts=_p3_ts)
_p3map = L.post_group_map()
_p3a = _p3map.get(("gP3a", _p3_pk)) or {}
_p3b = _p3map.get(("gP3b", _p3_pk)) or {}
_p3c = _p3map.get(("gP3c", _p3_pk)) or {}
check("P3 parcial: el resultado presente queda ok con su prueba",
      _p3a.get("status") == "ok" and _p3a.get("proof") == "https://x/prueba", _p3a)
check("P3 parcial: gid no visto -> unknown",
      _p3b.get("status") == "unknown" and _p3c.get("status") == "unknown",
      (_p3b, _p3c))
check("P3 parcial: proof timeout_parcial y batch limpio",
      _p3b.get("proof") == '{"reason":"timeout_parcial"}'
      and _p3b.get("batch") == "", _p3b)
check("P3 parcial: unresolved solo los no vistos",
      _p3["unresolved"] == ["gP3b", "gP3c"], _p3["unresolved"])
check("P3 parcial: senal timeout_parcial registrada",
      any(s.get("kind") == "timeout_parcial" for s in L.signals(limit=200)))

# 23. P4: REINTENTAR no borra un intento con publicacion o parcial -----------
AP.save_attempts({})
_u23 = "https://www.facebook.com/groups/23/posts/230/"
_k23 = AP.permalink_key(_u23)
_at23 = int(time.time() * 1000)
AP.save_attempts({_k23: {"state": "error", "published": 2, "unresolved": 0,
                         "at": _at23}})
check("P4 clear_attempt con published>0 -> False",
      AP.clear_attempt(_u23) is False)
check("P4 published>0: no borra el intento", _k23 in AP.load_attempts())
check("P4 published>0: senal retry_rechazado/ya_publicado",
      any(s.get("kind") == "retry_rechazado" and "ya_publicado" in str(s.get("detail"))
          for s in L.signals(limit=200)))
AP.save_attempts({_k23: {"state": "error", "published": 0, "unresolved": 1,
                         "at": _at23}})
check("P4 clear_attempt con unresolved>0 -> False",
      AP.clear_attempt(_u23) is False)
check("P4 unresolved>0: no borra el intento", _k23 in AP.load_attempts())
check("P4 unresolved>0: senal retry_rechazado/parcial_sin_confirmar",
      any(s.get("kind") == "retry_rechazado"
          and "parcial_sin_confirmar" in str(s.get("detail"))
          for s in L.signals(limit=200)))
AP.save_attempts({_k23: {"state": "error", "published": 0, "unresolved": 0,
                         "at": _at23}})
check("P4 clear_attempt limpio -> True", AP.clear_attempt(_u23) is True)
check("P4 clear_attempt limpio: borra el intento", _k23 not in AP.load_attempts())
AP.save_attempts({_k23: {"state": "error", "published": 3, "unresolved": 0,
                         "at": _at23}})
check("P4 force=True salta la guarda y borra", AP.clear_attempt(_u23, force=True) is True)
check("P4 force=True: intento borrado", _k23 not in AP.load_attempts())
check("P4 force=True: senal retry_forzado",
      any(s.get("kind") == "retry_forzado" for s in L.signals(limit=200)))

# 24. P5: active_halt fail-closed ------------------------------------------
try:
    os.remove(AP.HALT)
except OSError:
    pass
_t24 = int(time.time() * 1000)
check("P5 active_halt sin archivo -> no hay halt",
      AP.active_halt(_t24) == {}, AP.active_halt(_t24))
with open(AP.HALT, "w", encoding="utf-8") as _f:
    _f.write("{ esto no es json")
check("P5 halt ilegible (JSON corrupto) -> HALTED",
      bool(AP.active_halt(_t24)) is True, AP.active_halt(_t24))
with open(AP.HALT, "w", encoding="utf-8") as _f:
    _json.dump([1, 2], _f)
check("P5 halt no-objeto -> HALTED", bool(AP.active_halt(_t24)) is True)
with open(AP.HALT, "w", encoding="utf-8") as _f:
    _json.dump({"reason": "sin until"}, _f)
check("P5 halt sin 'until' -> HALTED", bool(AP.active_halt(_t24)) is True)
with open(AP.HALT, "w", encoding="utf-8") as _f:
    _json.dump({"until": "ayer"}, _f)
check("P5 halt con 'until' invalido -> HALTED", bool(AP.active_halt(_t24)) is True)
with open(AP.HALT, "w", encoding="utf-8") as _f:
    _json.dump({"until": _t24 - 1000}, _f)
check("P5 halt valido ya vencido -> no hay halt",
      AP.active_halt(_t24) == {}, AP.active_halt(_t24))
with open(AP.HALT, "w", encoding="utf-8") as _f:
    _json.dump({"until": _t24 + 60000, "reason": "prueba"}, _f)
check("P5 halt valido vigente -> HALTED", bool(AP.active_halt(_t24)) is True)
check("P5 halt ilegible registra senal",
      any(s.get("kind") == "halt_ilegible" for s in L.signals(limit=200)))
try:
    os.remove(AP.HALT)
except OSError:
    pass

# 25. P5: los JS incluyen los terminos ingleses de bloqueo ------------------
_lw25 = AP.JS_WARN.lower()
_lb25 = AP.JS_BLOCK.lower()
for _term in ("temporarily blocked", "blocked", "temporarily restricted",
              "restricted"):
    check("P5 JS_WARN incluye '%s'" % _term, _term in _lw25)
for _term in ("temporarily blocked", "blocked", "restricted"):
    check("P5 JS_BLOCK incluye '%s'" % _term, _term in _lb25)

# 26. P6: un item sin `dry` es simulacion ----------------------------------
check("P6 is_dry({}) es True", AP.is_dry({}) is True)
check("P6 is_dry(dry=False) es False", AP.is_dry({"dry": False}) is False)
check("P6 is_dry(dry=True) es True", AP.is_dry({"dry": True}) is True)
check("P6 payload sin dry -> simulacion",
      AP.share_cmd_payload({"url": "u"}, [], "pk").get("dry") is True)
check("P6 status public_items sin dry -> simulacion",
      AP.public_items([{"url": "u"}])[0].get("dry") is True)
_orig_run26 = AP.run_share
_orig_status26 = AP.STATUS
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.run_share = lambda item: {"results": [{"ok": True}]}
_st26 = AP.default_status()
AP.roll_day(_st26, "2026-01-01")
_out26 = AP._publish(_st26, {}, [],
                     {"url": "https://www.facebook.com/groups/26/posts/260/",
                      "state": "pendiente"}, 1_000_000_000_000)
check("P6 sin dry: NO publica real (done_today 0)", _st26.get("done_today") == 0,
      _st26.get("done_today"))
check("P6 sin dry: la ronda se reporta como simulacion",
      _out26.get("dry") is True, _out26)
AP.run_share = _orig_run26
AP.STATUS = _orig_status26

# 27. P7: presupuesto minimo del hijo 4800 s -------------------------------
check("P7 share_timeout(1 grupo) >= 4800", AP.share_timeout({}, 1) >= 4800,
      AP.share_timeout({}, 1))
check("P7 share_timeout(10 grupos) >= 4800", AP.share_timeout({}, 10) >= 4800,
      AP.share_timeout({}, 10))
check("P7 share_timeout(default) >= 4800", AP.share_timeout({}) >= 4800,
      AP.share_timeout({}))

# 28. P7: un kick no colapsa la pausa (next_at >= ahora+60 s) ---------------
_orig_health28 = AP.health
_orig_tab28 = AP._dedicated_tab
_orig_cp28 = AP.checkpoint_now
_orig_rq28 = AP.read_queue
AP.health = lambda: {"enabled": True, "share_alive": False,
                     "share_post_alive": False}
AP._dedicated_tab = lambda: None
AP.checkpoint_now = lambda tab: ""
AP.read_queue = lambda: {"paused": False, "items": []}
_st28 = AP.default_status()
_st28["next_at"] = 0
_cmd28 = os.path.join(_TMP, "kick.cmd.json")
with open(_cmd28, "w", encoding="utf-8") as _f:
    _json.dump({"action": "next"}, _f)
_t28 = int(time.time() * 1000)
AP._tick(_st28, _cmd28)
check("P7 kick (cmd next): next_at >= ahora+60000",
      int(_st28.get("next_at", 0)) >= _t28 + 60000,
      (_t28, _st28.get("next_at")))
AP.health = _orig_health28
AP._dedicated_tab = _orig_tab28
AP.checkpoint_now = _orig_cp28
AP.read_queue = _orig_rq28

# 29. P7: reconcile llama ensure_group_attempt tras cada result -----------
_orig_ensure29 = L.ensure_group_attempt
_calls29 = []


def _spy29(gid, ts=None, db=None):
    _calls29.append((str(gid), ts))
    return _orig_ensure29(gid, ts, db=db)


L.ensure_group_attempt = _spy29
try:
    _t29 = int(time.time() * 1000)
    _g29 = "gP729"
    _pk29 = "post:7:729"
    L.intent([{"gid": _g29, "post_key": _pk29, "ts": _t29}], batch="bP7test")
    _out29 = AP.reconcile_ledger(evidence_fn=lambda row: None, use_browser=False)
finally:
    L.ensure_group_attempt = _orig_ensure29
check("P7 reconcile llama ensure_group_attempt(gid, ts)",
      any(g == _g29 and isinstance(ts, int) for g, ts in _calls29), _calls29)
check("P7 reconcile resuelve el inflight a unknown",
      int(_out29.get("unknown", 0)) >= 1, _out29)
_p7grp = L.groups_map().get(_g29) or {}
check("P7 reconcile deja el grupo contado (day_n >= 1)",
      int(_p7grp.get("day_n") or 0) >= 1, _p7grp)

# 30. FIX 1: `exclude_gids` saca parejas ya cubiertas de la ronda -----------
#     12 relevantes: 10 `ok` de hace 7 h (cooldown vencido), g11 aun en warm y
#     g12 sin cubrir. Sin exclusion el selector re-publicaria los 10 `ok`.
_now30 = 1_800_000_000_000
_pk30 = "post:1:3030"
_gm30 = {}
_pm30 = {}
for _i in range(1, 13):
    _gm30["g%02d" % _i] = {"name": "Bazar Vecinos", "status": "ok",
                           "last_any": 0, "warm_at": 0, "status_until": 0,
                           "day_n": 0, "day_utc": ""}
for _i in range(1, 11):
    _pm30[("g%02d" % _i, _pk30)] = {"status": "ok",
                                    "last_ts": _now30 - 7 * 3600 * 1000}
_gm30["g11"]["warm_at"] = _now30 + DAY
_covered30 = ["g%02d" % _i for _i in range(1, 11)]
_sel30 = AP.select_round(_gm30, _pm30, _pk30, _now30, maxgroups=10,
                         exclude_gids=_covered30)
check("exclude_gids: no devuelve parejas ya ok",
      _sel30["gids"] == ["g12"], _sel30["gids"])
check("exclude_gids: reporta pair:covered por cada una",
      len([d for d in _sel30["discarded"]
           if d["reason"] == "pair:covered"]) == 10, _sel30["discarded"])
_sel30b = AP.select_round(_gm30, _pm30, _pk30, _now30, maxgroups=20)
check("sin exclude_gids: ok tras cooldown SI se re-selecciona (F1-11)",
      set(_covered30).issubset(set(_sel30b["gids"]))
      and "g12" in _sel30b["gids"], _sel30b["gids"])

# 31. FIX 2: una ronda TERMINADA sin `results` sella unknown, no inflight ---
_DB31 = os.path.join(_TMP, "f31.db")
L.init(_DB31)
_t31 = 1_800_000_000_000
_pk31 = "post:1:3131"
_gids31 = ["g31a", "g31b"]
L.intent([{"gid": _g, "post_key": _pk31, "ts": _t31} for _g in _gids31],
         batch="b31", db=_DB31)
AP.apply_round_results(_gids31, _pk31,
                       {"ok": False, "error": "share_post no dejo estado"},
                       batch="b31", ts=_t31, db=_DB31)
_pm31 = L.post_group_map(_DB31)
check("ronda sin results: no queda ningun inflight",
      L.inflight(db=_DB31) == [], L.inflight(db=_DB31))
check("ronda sin results: todas las filas quedan unknown",
      all((_pm31.get((_g, _pk31)) or {}).get("status") == "unknown"
          for _g in _gids31), _pm31)

# 32. FIX 3: `apply_round_results` no vuelve a contar el intento (P2) -------
_DB32 = os.path.join(_TMP, "f32.db")
L.init(_DB32)
_t32 = 1_800_000_000_000
_pk32 = "post:1:3232"
_gid32 = "g32"
L.group_state(_gid32, name="Bazar Vecinos", db=_DB32)
L.intent([{"gid": _gid32, "post_key": _pk32, "ts": _t32}], batch="b32", db=_DB32)
_gm32b = L.groups_map(_DB32)
check("intent ya deja day_n=1",
      int((_gm32b.get(_gid32) or {}).get("day_n") or 0) == 1, _gm32b)
AP.apply_round_results([_gid32], _pk32,
                       {"results": [{"gid": _gid32, "status": "ok",
                                     "ok": True, "proof": "p"}]},
                       batch="b32", ts=_t32, db=_DB32)
_gm32 = L.groups_map(_DB32)
check("tras intent + apply el MISMO intento sigue day_n=1",
      int((_gm32.get(_gid32) or {}).get("day_n") or 0) == 1, _gm32)

# 33. Residual gate: el camino diferido NO re-selecciona en bucle ----------
#     (i) TODOS los candidatos cubiertos por cobertura (excluidos) => el
#     next_at usa el suelo now + INTERVAL_MIN*60000, nunca `now` (que `_tick`
#     subia a now+60 s provocando re-seleccion sin publicar).
_now33 = 1_800_000_000_000
_pk33 = "post:1:3333"
_gm33 = {}
_pm33 = {}
for _i in range(1, 4):
    _gm33["c%02d" % _i] = {"name": "Bazar Vecinos", "status": "ok",
                           "last_any": 0, "warm_at": 0, "status_until": 0,
                           "day_n": 0, "day_utc": ""}
    # `ok` de hace 7 h: cooldown vencido, serian elegibles SIN exclusion.
    _pm33[("c%02d" % _i, _pk33)] = {"status": "ok",
                                    "last_ts": _now33 - 7 * 3600 * 1000}
_floor33 = int(AP.INTERVAL_MIN * 60000)
_sel33 = AP.select_round(_gm33, _pm33, _pk33, _now33, maxgroups=10,
                         exclude_gids=["c%02d" % _i for _i in range(1, 4)])
check("33 todos cubiertos: ronda diferida y sin gids",
      _sel33["deferred"] is True and _sel33["gids"] == [], _sel33)
check("33 todos cubiertos: next_at >= now + INTERVAL_MIN*60000",
      int(_sel33["next_at"]) >= _now33 + _floor33, (_sel33["next_at"], _now33))
check("33 todos cubiertos: next_at NO es now",
      int(_sel33["next_at"]) != _now33, _sel33["next_at"])

#     (i bis) Candidato NO excluido pero inelegible por enfriamiento corto: el
#     suelo evita igualmente un next_at por debajo del intervalo minimo.
_gm33b = {"b01": {"name": "Bazar Vecinos", "status": "ok", "last_any": 0,
                  "warm_at": 0, "status_until": 0, "day_n": 0, "day_utc": ""}}
_pm33b = {("b01", _pk33): {"status": "ok",
                           "last_ts": _now33 - (AP.COOLDOWN_POST - 30000)}}
_sel33b = AP.select_round(_gm33b, _pm33b, _pk33, _now33, maxgroups=10,
                          exclude_gids=[])
check("33 inelegible por cooldown corto: next_at >= now + INTERVAL_MIN*60000",
      _sel33b["deferred"] is True
      and int(_sel33b["next_at"]) >= _now33 + _floor33, _sel33b)

#     (ii) Al menos un grupo NO excluido con enfriamiento real: next_at es su
#     `free_at` exacto (30 min > suelo, asi el suelo no lo altera).
_gm33c = {}
_pm33c = {}
for _i in range(1, 4):
    _gm33c["d%02d" % _i] = {"name": "Bazar Vecinos", "status": "ok",
                            "last_any": 0, "warm_at": 0, "status_until": 0,
                            "day_n": 0, "day_utc": ""}
    _pm33c[("d%02d" % _i, _pk33)] = {"status": "ok",
                                     "last_ts": _now33 - 7 * 3600 * 1000}
_free33 = _now33 + 30 * 60000
_gm33c["d04"] = {"name": "Bazar Vecinos", "status": "ok", "last_any": 0,
                 "warm_at": 0, "status_until": 0, "day_n": 0, "day_utc": ""}
_pm33c[("d04", _pk33)] = {"status": "ok",
                          "last_ts": _free33 - AP.COOLDOWN_POST}
_sel33c = AP.select_round(_gm33c, _pm33c, _pk33, _now33, maxgroups=10,
                          exclude_gids=["d%02d" % _i for _i in range(1, 4)])
check("33 un grupo libre: next_at = free_at real del grupo",
      _sel33c["deferred"] is True and int(_sel33c["next_at"]) == _free33,
      (_sel33c["next_at"], _free33))

# 34. Aviso de arranque: ledger vacio con publicaciones previas -------------
_DB34 = os.path.join(_TMP, "f34.db")
L.init(_DB34)
_att34_before = AP.ATTEMPTS
AP.ATTEMPTS = os.path.join(_TMP, "f34_attempts.json")
try:
    with open(AP.ATTEMPTS, "w", encoding="utf-8") as _f:
        _json.dump({"u1": {"state": "hecho", "published": 3},
                    "u2": {"state": "hecho", "published": 2},
                    "u3": {"state": "error", "published": 0}}, _f)
    _n34 = AP.warn_empty_ledger_history(db=_DB34)
    check("34 ledger vacio + published>0 => N = suma de published",
          _n34 == 5, _n34)
    L.group_state("cw", name="Bazar Vecinos", db=_DB34)
    L.result("cw", "post:1:3400", "ok", True, proof="p", db=_DB34)
    check("34 ledger con filas en post_group => sin aviso (N=0)",
          AP.warn_empty_ledger_history(db=_DB34) == 0,
          AP.warn_empty_ledger_history(db=_DB34))
    _n34b = AP.warn_empty_ledger_history(db=os.path.join(_TMP, "no-existe.db"))
    check("34 ledger inexistente => sin aviso (N=0)", _n34b == 0, _n34b)
finally:
    AP.ATTEMPTS = _att34_before

for _name, _before in _REAL_STATE_BEFORE.items():
    _after = _snap(_name)
    check("unit no creo ni toco ~/.cxt/%s" % _name,
          _after == _before, (_before, _after))

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
