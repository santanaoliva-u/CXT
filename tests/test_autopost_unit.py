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
import time

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

# 7b. fbid de foto: sin este fix el worker veia pending=0 (dedupe tiraba items)
_fa = "https://www.facebook.com/photo/?fbid=1668053538331415&set=a.806895547780556&__cft__%5B0%5D=aaa"
_fb = "https://www.facebook.com/photo/?fbid=122237246654329580&set=pcb.122237247752329580&__cft__%5B0%5D=bbb"
_fc = "https://www.facebook.com/photo/?fbid=122237246654329580&set=pcb.122237247752329580&__cft__%5B0%5D=ccc-distinto"
check("fotos con fbid distintos NO colapsan",
      AP.permalink_key(_fa) != AP.permalink_key(_fb),
      (AP.permalink_key(_fa), AP.permalink_key(_fb)))
check("mismo fbid con __cft__ distinto SI es la misma clave",
      AP.permalink_key(_fb) == AP.permalink_key(_fc),
      (AP.permalink_key(_fb), AP.permalink_key(_fc)))
ded_photos = AP.dedupe_items([{"url": _fa}, {"url": _fb}, {"url": _fc}])
check("dedupe de 3 fotos -> 2 (solo el cft duplicado)",
      len(ded_photos) == 2, len(ded_photos))

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
check("item_key dry agrega sufijo :dry",
      AP.item_key({"url": _u, "dry": True}) == AP.permalink_key(_u) + ":dry",
      AP.item_key({"url": _u, "dry": True}))
check("item_key real NO lleva sufijo",
      AP.item_key({"url": _u, "dry": False}) == AP.permalink_key(_u),
      AP.item_key({"url": _u, "dry": False}))

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
#     Un dry NO debe marcar la clave real a-lo-mas-una-vez: si lo hiciera, un
#     share REAL posterior del MISMO permalink quedaria silenciosamente
#     saltado. Usa clave con sufijo ":dry" para que el fallo se persista.
_orig_run_share18 = AP.run_share
_orig_status18 = AP.STATUS
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.save_attempts({})
AP.run_share = lambda item: {"results": [{"ok": True}]}

_u18 = "https://www.facebook.com/groups/77/posts/770/"
_key18 = AP.permalink_key(_u18)
_key18_dry = AP.item_key({"url": _u18, "dry": True})

# 18.1 dry exitoso: en results como hecho, sin tope diario, sin intento real
_stdry = AP.default_status()
AP.roll_day(_stdry, "2026-01-01")
AP._publish(_stdry, {}, [], {"url": _u18, "dry": True, "state": "pendiente"},
            1_000_000_000_000)
check("18 dry ok: aparece en results como hecho",
      len(_stdry.get("results") or []) == 1 and _stdry["results"][0]["state"] == "hecho",
      _stdry.get("results"))
check("18 dry ok: NO incrementa done_today", _stdry.get("done_today") == 0, _stdry.get("done_today"))
_att18 = AP.load_attempts()
check("18 dry ok: el permalink real NO queda en intentos",
      _key18 not in _att18, _att18)
check("18 dry ok: la clave :dry SI queda hecho (evita reintento infinito)",
      (_att18.get(_key18_dry) or {}).get("state") == "hecho", _att18)

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

# 18.5 un dry FALLIDO se persiste como error (no se reintenta para siempre)
AP.save_attempts({})
AP.run_share = lambda item: {"results": [{"ok": False}]}
_u18f = "https://www.facebook.com/groups/78/posts/780/"
_stdryf = AP.default_status()
AP.roll_day(_stdryf, "2026-01-01")
AP._publish(_stdryf, {}, [], {"url": _u18f, "dry": True, "state": "pendiente"},
            3_000_000_000_000)
check("18 dry fail: item marcado error en results",
      _stdryf["results"][0]["state"] == "error", _stdryf.get("results"))
_att18f = AP.load_attempts()
check("18 dry fail: clave :dry queda error (no se re-pick)",
      (_att18f.get(AP.item_key({"url": _u18f, "dry": True})) or {}).get("state") == "error",
      _att18f)
_it18f = {"url": _u18f, "dry": True, "state": "pendiente"}
AP.recover_states([_it18f], AP.load_attempts())
check("18 dry fail: recover_states deja el dry en error",
      _it18f["state"] == "error", _it18f)
check("18 dry fail: el permalink real NO queda marcado (share real posible)",
      AP.permalink_key(_u18f) not in AP.load_attempts(), AP.load_attempts())

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

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
