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
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "scripts")):
    if os.path.exists(os.path.join(_p, "autopost.py")):
        sys.path.insert(0, _p)
        break

import autopost as AP  # noqa: E402

# Aislamos los archivos que toca el worker para no leer/escribir en ~/.cxt.
_TMP = tempfile.mkdtemp(prefix="autopost_unit_")
AP.ATTEMPTS = os.path.join(_TMP, "autopost_attempts.json")
AP.QUEUE = os.path.join(_TMP, "autoqueue.json")

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
check("CXT_AUTO_MAXDAY=20", AP.MAXDAY == 20, AP.MAXDAY)
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

# 9. caducidad de items (7 dias) -----------------------------------------
now_ms = 10_000_000_000_000
check("item de 1 dia NO caduca", AP.is_expired({"at": now_ms - 1 * DAY}, now_ms) is False)
check("item de 6 dias NO caduca", AP.is_expired({"at": now_ms - 6 * DAY}, now_ms) is False)
check("item de 8 dias SI caduca", AP.is_expired({"at": now_ms - 8 * DAY}, now_ms) is True)
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
check("checkpoint -> pausado True", cst.get("pausado") is True, cst)
check('checkpoint -> error "cuenta restringida"', cst.get("error") == "cuenta restringida", cst.get("error"))
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
      _att[AP.permalink_key(_u)]["state"] == "publicando", _att)
_it = {"url": _u, "state": "pendiente"}
AP.recover_states([_it], AP.load_attempts())
check("item marcado publicando -> hecho al reiniciar (no re-share)", _it["state"] == "hecho", _it)
AP.attempt_mark(AP.item_key({"url": _u}), "error", "boom")
_it2 = {"url": _u, "state": "pendiente"}
AP.recover_states([_it2], AP.load_attempts())
check("item marcado error -> error con su motivo", _it2["state"] == "error" and _it2["error"] == "boom", _it2)
AP.attempt_mark(AP.item_key({"url": _u}), "hecho")
_it3 = {"url": _u, "state": "pendiente"}
AP.recover_states([_it3], AP.load_attempts())
check("item hecho en attempts -> hecho (no re-share)", _it3["state"] == "hecho", _it3)
_it4 = {"url": "https://www.facebook.com/groups/8/posts/80/", "state": "pendiente"}
AP.recover_states([_it4], AP.load_attempts())
check("item no intentado sigue pendiente", _it4["state"] == "pendiente", _it4)
check("item_key usa el permalink normalizado",
      AP.item_key({"url": _u + "?ref=share"}) == AP.permalink_key(_u),
      AP.item_key({"url": _u + "?ref=share"}))

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

AP.run_share = lambda item: {"results": [{"ok": False}]}
_fail = _publish_fresh({"url": "https://www.facebook.com/groups/1/posts/3/",
                        "dry": False, "state": "pendiente"})
check("fallo real: NO incrementa done_today", _fail.get("done_today") == 0, _fail.get("done_today"))

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

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
