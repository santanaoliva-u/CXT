#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon, SIN ledger real) de:
  - `last_run` (plan 5.6): mensajes exactos, conteos e items capados
  - metricas disponibles sin inventar valores
  - perfil FIJADO (precedencia, cache y perfil no disponible)
  - `_dedicated_tab` con CXT_BACKGROUND=1 (pestana propia, sin tab.activate)
  - `run_share` sin guardar/restaurar pestanas en modo fondo (rollback con 0)

Uso: python3 test_last_run_unit.py   (exit 0 = PASS)
"""
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_HERE, "autopost.py")):
    sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="autopost_lastrun_")
os.environ["CXT_DIR"] = _TMP

import autopost as AP  # noqa: E402

AP.DIR = _TMP
AP.QUEUE = os.path.join(_TMP, "autoqueue.json")
AP.ATTEMPTS = os.path.join(_TMP, "autopost_attempts.json")
AP.STATUS = os.path.join(_TMP, "autopost_status.json")
AP.SHARE_CMD = os.path.join(_TMP, "share_post.cmd.json")
AP.SHARE_STATUS = os.path.join(_TMP, "share_post_status.json")
AP.GROUPS = os.path.join(_TMP, "groups.json")
AP.RELEVANCE = os.path.join(_TMP, "relevance.json")
AP.LOCK = os.path.join(_TMP, "browser.lock")
AP.HALT = os.path.join(_TMP, "autopost.halt.json")

fails = []
_ENV_KEYS = ("CXT_BACKGROUND", "CXT_TAB", "CXT_PROFILE_DIR", "CXT_PROFILE_NAME",
             "CXT_FAST", "CXT_LOCK_HELD")


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name
          + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


class env_guard:
    """Aisla el entorno: guarda/restaura las banderas que tocan estos tests."""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k in _ENV_KEYS:
            self.saved[k] = os.environ.pop(k, None)
        for k, v in self.kw.items():
            if v is not None:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *_exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _row(gid, ok=True, status=None, proof="", reason=""):
    return {"gid": gid, "ok": ok,
            "status": status or ("ok" if ok else "unknown"),
            "proof": proof, "reason": reason}


# 1. last_run: ronda completa ok ----------------------------------------
_lr = AP.build_last_run(
    "run-1", 1000, {"results": [_row("g1", proof="https://fb/p/1"),
                                _row("g2", proof="https://fb/p/2"),
                                _row("g3", proof="https://fb/p/3")]},
    ["g1", "g2", "g3"], "fast", profile="/home/x/.config/chrome/Default",
    profile_name="Jesús", finished_at=9000,
    metrics={"lock_wait_ms": 4}, names={"g2": "Bazar Vecinos"})
check("last_run ok: shared=3 failed=0 skipped=0",
      (_lr["shared"], _lr["failed"], _lr["skipped"]) == (3, 0, 0), _lr)
check("last_run ok: ok=True", _lr["ok"] is True, _lr["ok"])
check("last_run ok: mensaje exacto del plan 5.6",
      _lr["message"] == "¡Listo! Se compartió en 3 grupos.", _lr["message"])
check("last_run ok: items con group/url/ok/post_url/reason",
      _lr["items"][1] == {"group": "Bazar Vecinos", "url": "", "ok": True,
                          "post_url": "https://fb/p/2", "reason": ""},
      _lr["items"][1])
check("last_run ok: perfil y nombres persistidos",
      (_lr["profile"], _lr["profile_name"]) == ("/home/x/.config/chrome/Default", "Jesús"),
      (_lr["profile"], _lr["profile_name"]))
check("last_run ok: duration_ms real (fin - inicio)",
      _lr["metrics"]["duration_ms"] == 8000, _lr["metrics"]["duration_ms"])
check("last_run ok: publish_* derivados de los conteos",
      (_lr["metrics"]["publish_success"], _lr["metrics"]["publish_failed"],
       _lr["metrics"]["publish_skipped"]) == (3, 0, 0), _lr["metrics"])
check("last_run ok: lock_wait_ms medido se conserva",
      _lr["metrics"]["lock_wait_ms"] == 4, _lr["metrics"]["lock_wait_ms"])
check("last_run ok: metricas no medidas quedan en None (no se inventan)",
      _lr["metrics"]["group_total_ms"] is None
      and _lr["metrics"]["cxt_calls_per_group"] is None, _lr["metrics"])

# 2. last_run: ronda parcial (timeout / alertas) -------------------------
_lr2 = AP.build_last_run(
    "run-2", 1000, {"results": [_row("g1", proof="p1"),
                                _row("g2", proof="p2"),
                                _row("g3", proof="p3"),
                                _row("g4", ok=False, status="pending"),
                                _row("g5", ok=False, status="blocked",
                                     reason="bloqueo")]},
    ["g1", "g2", "g3", "g4", "g5"], "normal", finished_at=5000)
check("last_run parcial: shared=3 skipped=1 failed=1",
      (_lr2["shared"], _lr2["skipped"], _lr2["failed"]) == (3, 1, 1), _lr2)
check("last_run parcial: ok=False", _lr2["ok"] is False, _lr2["ok"])
check("last_run parcial: mensaje 'N de M; K con alerta' (K = sin compartir)",
      _lr2["message"] == "Se compartió en 3 de 5 grupos; 2 con alerta.",
      _lr2["message"])
check("last_run parcial: pendiente cuenta skipped y no failed",
      _lr2["items"][3]["reason"] == "pending", _lr2["items"][3])
check("last_run parcial: fallo conserva reason",
      _lr2["items"][4]["reason"] == "bloqueo", _lr2["items"][4])

# 3. last_run: fallo total ----------------------------------------------
_lr3 = AP.build_last_run("run-3", 0, {"results": [_row("g1", ok=False),
                                                  _row("g2", ok=False)]},
                         ["g1", "g2"], "normal", finished_at=10,
                         error="sin evidencia")
check("last_run fallo total: mensaje del plan 5.6",
      _lr3["message"] == "No se pudo compartir. Revisa la alerta.", _lr3["message"])
check("last_run fallo total: ok=False y failed=2",
      _lr3["ok"] is False and _lr3["failed"] == 2, (_lr3["ok"], _lr3["failed"]))

# 4. last_run: dry-run nunca dice que compartio -------------------------
_lr4 = AP.build_last_run("run-4", 0, {"results": []}, ["g1", "g2", "g3", "g4"],
                         "dry", finished_at=10, dry=True)
check("last_run dry: mensaje exacto sin publicar",
      _lr4["message"] == "Prueba completada: 4 grupos (sin publicar).",
      _lr4["message"])
check("last_run dry: no afirma que se compartio",
      "compart" not in _lr4["message"].lower(), _lr4["message"])
check("last_run dry: ok=True sin error", _lr4["ok"] is True, _lr4["ok"])
_lr4b = AP.build_last_run("run-4b", 0, {"results": []}, ["g1"], "dry",
                          finished_at=10, dry=True, error="timeout")
check("last_run dry con error: ok=False", _lr4b["ok"] is False, _lr4b["ok"])
check("last_run dry con error: mensaje sigue siendo de prueba",
      _lr4b["message"] == "Prueba completada: 1 grupos (sin publicar).",
      _lr4b["message"])

# 5. last_run: items capados pero conteos COMPLETOS ---------------------
_many = [str(i) for i in range(250)]
_lr5 = AP.build_last_run("run-5", 0,
                         {"results": [_row(g, proof="p") for g in _many]},
                         _many, "fast", finished_at=10)
check("last_run cap: items <= 200", len(_lr5["items"]) == AP.LAST_RUN_ITEMS_CAP,
      len(_lr5["items"]))
check("last_run cap: shared cuenta los 250 (no solo los capados)",
      _lr5["shared"] == 250, _lr5["shared"])
check("last_run cap: mensaje con el total real",
      _lr5["message"] == "¡Listo! Se compartió en 250 grupos.", _lr5["message"])

# 6. perfil fijado: precedencia, cache y no disponible -------------------
with env_guard(CXT_PROFILE_DIR="/tmp", CXT_PROFILE_NAME="Jesús"):
    AP.reset_profile_pin()
    _pin = AP.profile_pin({})
    check("perfil: env manda (dir + name)",
          (_pin["profile_dir"], _pin["profile_name"], _pin["available"])
          == ("/tmp", "Jesús", True), _pin)
    _pin2 = AP.profile_pin({"profile_dir": "/otro", "profile_name": "Otro"})
    check("perfil: un job NUNCA cambia de perfil (cache)",
          _pin2["profile_dir"] == "/tmp" and _pin2["profile_name"] == "Jesús",
          _pin2)

AP.reset_profile_pin()
with env_guard():
    _pin3 = AP.profile_pin({"profile_dir": "/tmp", "profile_name": "desde-status"})
    check("perfil: sin env se usa el status",
          (_pin3["profile_dir"], _pin3["profile_name"]) == ("/tmp", "desde-status"),
          _pin3)
AP.reset_profile_pin()
with env_guard():
    _pin4 = AP.profile_pin({"profile_name": "solo-etiqueta"})
    check("perfil: etiqueta sin dir no inventa ruta",
          (_pin4["profile_dir"], _pin4["profile_name"]) == ("", "solo-etiqueta"),
          _pin4)

AP.reset_profile_pin()
with env_guard(CXT_PROFILE_DIR="/no/existe/perfil-cxt"):
    _st_pin = {"alerta": {}}
    _pin5 = AP.profile_pin(_st_pin)
    check("perfil: ruta inexistente => available=False",
          _pin5["available"] is False, _pin5)
    check("perfil: ruta inexistente => ALERTA (sin cambiar de perfil)",
          "perfil no disponible: /no/existe/perfil-cxt"
          == (_st_pin.get("alerta") or {}).get("reason"),
          _st_pin.get("alerta"))
    check("perfil: la alerta queda visible en error",
          str(_st_pin.get("error") or "").startswith("ALERTA: "), _st_pin.get("error"))

AP.reset_profile_pin()
with env_guard(CXT_PROFILE_DIR="Default"):
    _pin6 = AP.profile_pin({})
    check("perfil: nombre suelto ('Default') es etiqueta, no ruta",
          _pin6["available"] is True and _pin6["profile_dir"] == "Default", _pin6)
AP.reset_profile_pin()

# 7. CXT_BACKGROUND: bandera y pestana propia sin activar ---------------
with env_guard():
    check("CXT_BACKGROUND default = 1 (fondo)", AP.background_enabled() is True,
          AP.background_enabled())
with env_guard(CXT_BACKGROUND="0"):
    check("CXT_BACKGROUND=0 desactiva el modo fondo",
          AP.background_enabled() is False, AP.background_enabled())

_calls = []
_orig_ensure = getattr(AP.cxtlib, "ensure_background_tab", None)
_orig_call = AP.cxtlib.call
AP.reset_auto_tab()
with env_guard():
    AP.cxtlib.ensure_background_tab = lambda cxt=None: {"tabId": 42,
                                                        "windowId": 9,
                                                        "created": True}
    AP.cxtlib.call = lambda *a, **k: _calls.append((a, k)) or {"ok": True}
    try:
        _tab1 = AP._dedicated_tab()
        _tab2 = AP._dedicated_tab()
    finally:
        if _orig_ensure is None:
            del AP.cxtlib.ensure_background_tab
        else:
            AP.cxtlib.ensure_background_tab = _orig_ensure
        AP.cxtlib.call = _orig_call
check("pestana: ensure_background_tab devuelve el id", _tab1 == 42, _tab1)
check("pestana: la extension reutiliza el mismo id", _tab2 == 42, _tab2)
check("pestana: NUNCA se llama tab.activate",
      not [c for c in _calls if c[0] and c[0][1] == "tab.activate"], _calls)
_seq_calls = []
_orig_ensure2 = getattr(AP.cxtlib, "ensure_background_tab", None)
_orig_call2 = AP.cxtlib.call
AP.reset_auto_tab()
with env_guard():
    _ids = [{"tabId": 42}, {"tabId": 99}]
    AP.cxtlib.ensure_background_tab = lambda cxt=None: _seq_calls.append(1) or _ids.pop(0)
    AP.cxtlib.call = lambda *a, **k: {"ok": True}
    try:
        _stale1 = AP._dedicated_tab()
        _stale2 = AP._dedicated_tab()
    finally:
        if _orig_ensure2 is None:
            del AP.cxtlib.ensure_background_tab
        else:
            AP.cxtlib.ensure_background_tab = _orig_ensure2
        AP.cxtlib.call = _orig_call2
check("pestana: primer id valido", _stale1 == 42, _stale1)
check("pestana: valida el id en cada ronda", _stale2 == 99, _stale2)
check("pestana: no confia en un id cacheado muerto", len(_seq_calls) == 2,
      _seq_calls)
AP.reset_auto_tab()

# 8. run_share en modo fondo: sin guardar/restaurar pestanas ------------
def _fake_child(cmd, budget, env):
    """Simula share_post: escribe un status fresco y nada mas."""
    with open(AP.SHARE_STATUS, "w", encoding="utf-8") as fh:
        json.dump({"ok": True, "results": [_row("g1", proof="p1")],
                   "finished_at": int(time.time() * 1000)}, fh)


def _run_share_case(**flags):
    """Ejecuta run_share con el hijo simulado y devuelve el estado del caso."""
    _saved = {"child": AP._run_share_child, "ded": AP._dedicated_tab,
              "active": AP.active_tabs, "restore": AP.restore_tabs}
    seen = {"active": 0, "restore": 0, "env": {}, "st": {}}
    AP._dedicated_tab = lambda st=None: 7
    AP.active_tabs = lambda: seen.__setitem__("active", seen["active"] + 1) or [1]
    AP.restore_tabs = lambda ids: seen.__setitem__("restore", seen["restore"] + 1)

    def _child(cmd, budget, env):
        seen["env"] = dict(env)
        _fake_child(cmd, budget, env)

    AP._run_share_child = _child
    try:
        with env_guard(**flags):
            AP.reset_profile_pin()
            AP.reset_auto_tab()
            out = AP.run_share({"url": "https://fb/post/1", "_gids": ["g1"],
                                "_lock_held": True})
    finally:
        AP._run_share_child = _saved["child"]
        AP._dedicated_tab = _saved["ded"]
        AP.active_tabs = _saved["active"]
        AP.restore_tabs = _saved["restore"]
        AP.reset_profile_pin()
    return out, seen, os.path.exists(AP.SHARE_CMD)


_c_out, _c_seen, _ = _run_share_case()
check("run_share fondo: hijo recibe CXT_TAB de la pestana propia",
      _c_seen["env"].get("CXT_TAB") == "7", _c_seen["env"].get("CXT_TAB"))
check("run_share fondo: hijo recibe CXT_BACKGROUND=1",
      _c_seen["env"].get("CXT_BACKGROUND") == "1",
      _c_seen["env"].get("CXT_BACKGROUND"))
check("run_share fondo: NO guarda pestañas activas", _c_seen["active"] == 0,
      _c_seen["active"])
check("run_share fondo: NO restaura pestañas (no roba foco)",
      _c_seen["restore"] == 0, _c_seen["restore"])

_rb_out, _rb_seen, _ = _run_share_case(CXT_BACKGROUND="0")
check("run_share rollback (0): guarda y restaura pestañas",
      (_rb_seen["active"], _rb_seen["restore"]) == (1, 1), _rb_seen)
check("run_share rollback (0): no fuerza modo fondo en el hijo",
      _rb_seen["env"].get("CXT_BACKGROUND") != "1",
      _rb_seen["env"].get("CXT_BACKGROUND"))

_ok_no_child = {"n": 0}


def _child_should_not_run(cmd, budget, env):
    _ok_no_child["n"] += 1


_saved_child2 = AP._run_share_child
AP._run_share_child = _child_should_not_run
try:
    with env_guard(CXT_PROFILE_DIR="/no/existe/perfil-cxt"):
        AP.reset_profile_pin()
        _st_bad = {"alerta": {}}
        _bad = AP.run_share({"url": "https://fb/post/1", "_gids": ["g1"]})
        AP.profile_pin(_st_bad)
finally:
    AP._run_share_child = _saved_child2
    AP.reset_profile_pin()
check("run_share perfil no disponible: ronda fallida explicita",
      _bad.get("ok") is False
      and str(_bad.get("error") or "").startswith("perfil no disponible"),
      _bad)
check("run_share perfil no disponible: NO lanza share_post (no cambia de perfil)",
      _ok_no_child["n"] == 0, _ok_no_child["n"])
check("run_share perfil no disponible: alerta visible",
      bool((_st_bad.get("alerta") or {}).get("reason")), _st_bad.get("alerta"))

# 9. _record_last_run: pestana propia y perfil quedan en el status -----
with env_guard(CXT_PROFILE_DIR="/tmp", CXT_PROFILE_NAME="Jesús"):
    AP.reset_profile_pin()
    AP.reset_auto_tab()
    _saved_ded = AP._dedicated_tab
    _saved_names = AP._last_run_names
    AP._dedicated_tab = lambda: 7
    AP._last_run_names = lambda gids: {}
    try:
        _st_rec = {"alerta": {}}
        _rec = AP._record_last_run(
            _st_rec, {"url": "u", "fast": False}, ["g1"],
            {"results": [_row("g1", proof="p1")]}, 1000, "b1-x", False, 12,
            3, {"ok": True, "error": ""})
    finally:
        AP._dedicated_tab = _saved_ded
        AP._last_run_names = _saved_names
        AP.reset_profile_pin()
    check("_record_last_run: st['auto_tab'] = pestana de la ronda",
          _st_rec.get("auto_tab") == 7, _st_rec.get("auto_tab"))
    check("_record_last_run: st['profile_dir']/'profile_name' fijados",
          (_st_rec.get("profile_dir"), _st_rec.get("profile_name")) == ("/tmp", "Jesús"),
          _st_rec.get("profile_dir"))
    check("_record_last_run: last_run.mode y metricas en None hasta medir",
          _rec["mode"] == "normal"
          and _rec["metrics"]["status_bytes"] is None
          and _rec["metrics"]["status_write_ms"] is None, _rec["metrics"])
    check("_record_last_run: lock_wait_ms y edad de cola medidos",
          (_rec["metrics"]["lock_wait_ms"], _rec["metrics"]["enqueue_to_first_action_ms"])
          == (3, 12), _rec["metrics"])

# 10. _publish con perfil fijado inexistente: alerta, sin intent ni share --
_now_p = int(time.time() * 1000)
_saved_intent = AP.ledger.intent
_saved_child_p = AP._run_share_child
_intent_calls = []
_share_calls = []


def _intent_spy(*a, **k):
    _intent_calls.append(a)


def _share_spy(*a, **k):
    _share_calls.append(a)


AP.ledger.intent = _intent_spy
AP._run_share_child = _share_spy
try:
    with env_guard(CXT_PROFILE_DIR="/no/existe/perfil-cxt"):
        AP.reset_profile_pin()
        AP.reset_auto_tab()
        _st_p = AP.default_status()
        _item_p = {"url": "https://fb/post/9", "state": "pendiente",
                   "dry": False}
        _q_p = {"items": [_item_p], "paused": False}
        _out_p = AP._publish(_st_p, _q_p, [_item_p], _item_p, _now_p,
                             gids=["g1"], post_key="pl:9")
finally:
    AP.ledger.intent = _saved_intent
    AP._run_share_child = _saved_child_p
    AP.reset_profile_pin()
check("_publish perfil no disponible: estado error", _out_p.get("state") == "error",
      _out_p)
check("_publish perfil no disponible: NO escribe intent en el ledger",
      _intent_calls == [], _intent_calls)
check("_publish perfil no disponible: NO lanza share", _share_calls == [],
      _share_calls)
check("_publish perfil no disponible: alerta en el status",
      str(_st_p.get("error") or "").startswith("ALERTA: perfil no disponible"),
      _st_p.get("error"))
check("_publish perfil no disponible: last_run de error con failed=1",
      (_st_p.get("last_run") or {}).get("failed") == 1
      and (_st_p.get("last_run") or {}).get("ok") is False,
      _st_p.get("last_run"))
check("_publish perfil no disponible: mensaje de fallo del plan 5.6",
      (_st_p.get("last_run") or {}).get("message")
      == "No se pudo compartir. Revisa la alerta.",
      (_st_p.get("last_run") or {}).get("message"))
check("_publish perfil no disponible: no reintenta en bucle (next_at futuro)",
      int(_st_p.get("next_at") or 0) > _now_p, _st_p.get("next_at"))
with open(AP.STATUS, encoding="utf-8") as _fh:
    _disk_p = json.load(_fh)
check("_publish perfil no disponible: last_run persistido en disco",
      (_disk_p.get("last_run") or {}).get("profile")
      == "/no/existe/perfil-cxt", _disk_p.get("last_run"))

# 11. status: claves nuevas y last_run persistido ----------------------
_dflt = AP.default_status()
check("status: claves auto_tab/profile/last_run por defecto",
      (_dflt.get("auto_tab"), _dflt.get("profile_dir"),
       _dflt.get("profile_name"), _dflt.get("last_run")) == (None, "", "", {}),
      _dflt)
_st_w = AP.default_status()
_st_w["last_run"] = _lr
_ms_w, _size_w = AP._write_status_measured(_st_w)
with open(AP.STATUS, encoding="utf-8") as _fh:
    _disk = json.load(_fh)
check("status: last_run se persiste en autopost_status.json",
      (_disk.get("last_run") or {}).get("id") == "run-1",
      _disk.get("last_run"))
check("status: status_write_ms/status_bytes medidos de la escritura",
      isinstance(_ms_w, int) and _ms_w >= 0 and isinstance(_size_w, int)
      and _size_w > 0, (_ms_w, _size_w))

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
