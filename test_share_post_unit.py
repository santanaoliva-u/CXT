#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon) de share_post.py.

Cubre la logica pura del contrato F1 §3:
  - resolve_gids conserva el orden y no inventa nombres
  - un gid desconocido se salta como unknown / "gid desconocido"
  - status_entry normaliza evidencia (gid/proof/status/reason) y conserva las
    claves historicas del motor
  - pending/unknown se cuentan en el top-level
  - la regla dura: permalink de otro gid => unknown / "gid no coincide"
  - sin `gids`, select_groups (ids/filtro) sigue intacto
  - con el lock ocupado no se publica nada y el status sale con error

Uso: python3 test_share_post_unit.py   (exit 0 = PASS)
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import share_post as SP  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name
          + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


CATALOGUE = [
    {"id": "111", "name": "Grupo Once"},
    {"id": "222", "name": "Grupo Dos"},
    {"id": "333", "name": "Grupo Tres"},
]

# 1. resolve_gids: orden exacto y nombre resuelto ---------------------------
refs = SP.resolve_gids(["333", "111", "222"], CATALOGUE)
check("resolve_gids conserva el orden pedido",
      [r["gid"] for r in refs] == ["333", "111", "222"], refs)
check("resolve_gids resuelve id -> name",
      [r["name"] for r in refs] == ["Grupo Tres", "Grupo Once", "Grupo Dos"], refs)

# 1b. gid desconocido: se devuelve con name=None (no se inventa) ------------
refs = SP.resolve_gids(["999", "111"], CATALOGUE)
check("resolve_gids: gid desconocido -> name None",
      refs[0]["gid"] == "999" and refs[0]["name"] is None, refs[0])
check("resolve_gids: gid conocido conserva su nombre",
      refs[1]["gid"] == "111" and refs[1]["name"] == "Grupo Once", refs[1])

# 1c. entradas vacias no rompen ni desplazan el orden -----------------------
refs = SP.resolve_gids([" ", "", "222"], CATALOGUE)
check("resolve_gids ignora vacios y conserva el resto",
      [r["gid"] for r in refs] == ["222"], refs)

# 2. status_entry: campos y coherencia -------------------------------------
raw = {"group": "Grupo Once", "ok": True, "published": True, "stage": "ok",
       "label": "Publicar", "x": 10, "y": 20, "reason": "permalink-nuevo",
       "pending": False, "approval_url": ""}
e = SP.status_entry(raw, "111", "Grupo Once", "pl:abc")
for key in ("group", "gid", "ok", "proof", "status", "reason"):
    check("status_entry tiene %s" % key, key in e, sorted(e))
check("status_entry: exito -> status ok",
      e["status"] == "ok" and e["ok"] is True and e["gid"] == "111", e)
check("status_entry conserva claves historicas del motor",
      all(e.get(k) == raw[k] for k in raw), e)
check("status_entry: exito sin prueba -> proof vacio", e["proof"] == "", e["proof"])

# 2b. gid desconocido (name=None) => unknown, saltado ----------------------
e = SP.status_entry({}, "999", None, "pl:abc")
check("status_entry gid desconocido -> unknown",
      e["status"] == "unknown" and e["ok"] is False, e)
check("status_entry gid desconocido -> reason correcto",
      e["reason"] == "gid desconocido", e["reason"])
check("status_entry gid desconocido no inventa nombre",
      e["group"] == "" and e["gid"] == "999", e)

# 2c. pendiente -> pending, nunca ok ---------------------------------------
e = SP.status_entry({"ok": False, "pending": True, "reason": "pendiente-aprobacion"},
                    "222", "Grupo Dos", "pl:abc")
check("status_entry pendiente -> pending", e["status"] == "pending", e)
check("status_entry pendiente nunca es ok", e["ok"] is False, e)

# 2d. duda sin prueba -> unknown -------------------------------------------
e = SP.status_entry({"ok": False, "reason": "sin-prueba"}, "222", "Grupo Dos", "")
check("status_entry sin prueba -> unknown", e["status"] == "unknown", e)
check("status_entry sin prueba no asciende ok", e["ok"] is False, e)

# 3. proof propagado -------------------------------------------------------
url = "https://www.facebook.com/groups/111/posts/987654/"
e = SP.status_entry({"ok": True, "proof": url, "reason": "permalink-nuevo"},
                    "111", "Grupo Once", "pl:abc")
check("status_entry propaga proof (permalink)", e["proof"] == url, e["proof"])
check("status_entry proof de su gid -> ok", e["status"] == "ok", e)

# 3b. REGLA DURA: permalink de otro gid => unknown / "gid no coincide" ------
ajeno = "https://www.facebook.com/groups/999/posts/987654/"
e = SP.status_entry({"ok": True, "proof": ajeno, "reason": "permalink-nuevo"},
                    "111", "Grupo Once", "pl:abc")
check("gid no coincide -> status unknown", e["status"] == "unknown", e)
check("gid no coincide -> reason exacto", e["reason"] == "gid no coincide", e["reason"])
check("gid no coincide jamas ok", e["ok"] is False, e)

# 3c. el permalink ascendido (approval_url) tambien manda -------------------
e = SP.status_entry({"ok": True, "reason": "permalink-aprobado", "approval_url": ajeno},
                    "111", "Grupo Once", "pl:abc")
check("approval_url de otro gid -> unknown", e["status"] == "unknown", e)

# 4. conteos top-level -----------------------------------------------------
st = {"results": [
    {"status": "unknown"}, {"status": "unknown"}, {"status": "pending"},
    {"status": "ok"}, {"status": "blocked"}]}
SP._refresh_counts(st)
check("_refresh_counts unknown", st["unknown"] == 2, st["unknown"])
check("_refresh_counts pending", st["pending"] == 1, st["pending"])

# 5. camino viejo sin gids: select_groups intacto --------------------------
_orig_load = SP.load_groups
SP.load_groups = lambda: CATALOGUE
try:
    check("select_groups ids: orden y recorte",
          SP.select_groups("Grupo Tres, Grupo Once", "", 0)
          == ["Grupo Tres", "Grupo Once"],
          SP.select_groups("Grupo Tres, Grupo Once", "", 0))
    check("select_groups filtro por nombre",
          SP.select_groups("", "grupo", 0) == ["Grupo Once", "Grupo Dos", "Grupo Tres"],
          SP.select_groups("", "grupo", 0))
    check("select_groups filtro respeta limit",
          SP.select_groups("", "grupo", 2) == ["Grupo Once", "Grupo Dos"],
          SP.select_groups("", "grupo", 2))
    check("select_groups sin ids ni filtro -> vacio",
          SP.select_groups("", "", 0) == [], SP.select_groups("", "", 0))
    check("_gid_for_name resuelve nombre exacto",
          SP._gid_for_name("Grupo Dos", CATALOGUE) == "222",
          SP._gid_for_name("Grupo Dos", CATALOGUE))
    check("_gid_for_name acepta id numerico",
          SP._gid_for_name("333", CATALOGUE) == "333")
    check("_gid_for_name no adivina con nombre parcial",
          SP._gid_for_name("Grupo", CATALOGUE) == "",
          SP._gid_for_name("Grupo", CATALOGUE))
finally:
    SP.load_groups = _orig_load

# 6. lock ocupado: escribe error y NO publica ------------------------------
_orig_status = SP.STATUS
_orig_lock = SP.ledger.lock_browser
_orig_unlock = SP.ledger.unlock_browser
_orig_share = SP.share_url_group
_orig_env = os.environ.pop("CXT_LOCK_HELD", None)
_tmp = tempfile.mkdtemp(prefix="share_post_unit_")
_cmd = os.path.join(_tmp, "share_post.cmd.json")
with open(_cmd, "w", encoding="utf-8") as fh:
    json.dump({"url": "https://www.facebook.com/x", "gids": ["111"],
               "post_key": "pl:abc", "dry": True}, fh)
SP.STATUS = os.path.join(_tmp, "share_post_status.json")
SP.ledger.lock_browser = lambda *a, **k: None
SP.share_url_group = lambda *a, **k: (_ for _ in ()).throw(AssertionError("publico sin lock"))
try:
    SP.run_from_cmd(_cmd)
    with open(SP.STATUS, encoding="utf-8") as fh:
        busy = json.load(fh)
    check("lock ocupado -> running False", busy.get("running") is False, busy)
    check("lock ocupado -> error contractual",
          busy.get("error") == "browser ocupado por otro driver", busy.get("error"))
    check("lock ocupado -> no publica ni registra resultados",
          busy.get("results") == [] and busy.get("published") == 0, busy)
    check("lock ocupado -> conserva post_key y dry",
          busy.get("post_key") == "pl:abc" and busy.get("dry") is True, busy)
    check("lock ocupado -> conteos en cero",
          busy.get("unknown") == 0 and busy.get("pending") == 0, busy)
finally:
    SP.STATUS = _orig_status
    SP.ledger.lock_browser = _orig_lock
    SP.ledger.unlock_browser = _orig_unlock
    SP.share_url_group = _orig_share
    if _orig_env is not None:
        os.environ["CXT_LOCK_HELD"] = _orig_env

# 7. ronda completa con gids: orden, saltos y conteos (sin navegador) ------
_orig_ensure = SP.cxtlib.ensure_background_tab
_orig_reconcile = SP.shared.reconcile_approvals
_orig_close = SP.close_dialog
_orig_sleep = SP.sleep
_orig_status2 = SP.STATUS
_orig_load2 = SP.load_groups
_calls = []
_env_before = os.environ.get("CXT_LOCK_HELD")
os.environ["CXT_LOCK_HELD"] = "1"          # autopost ya tiene el lock: no se toca
SP.load_groups = lambda: CATALOGUE
SP.STATUS = os.path.join(_tmp, "share_post_status_full.json")
SP.cxtlib.ensure_background_tab = lambda *a, **k: 1
SP.close_dialog = lambda: None
SP.sleep = lambda ms: None
SP.shared.reconcile_approvals = lambda *a, **k: 0
SP.ledger.lock_browser = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("toco el lock con CXT_LOCK_HELD=1"))


def _fake_share(url, name, dry, text, expect="", mode=None):
    _calls.append(name)
    if name == "Grupo Tres":
        return {"ok": True, "published": True, "proof": "/groups/333/posts/1/"}
    if name == "Grupo Once":
        return {"ok": False, "pending": True, "reason": "pendiente-aprobacion"}
    return {"ok": True, "published": True, "proof": "/groups/111/posts/2/"}


SP.share_url_group = _fake_share
_cmd2 = os.path.join(_tmp, "share_post.cmd2.json")
with open(_cmd2, "w", encoding="utf-8") as fh:
    json.dump({"url": "https://www.facebook.com/x",
               "gids": ["333", "999", "111", "222"], "post_key": "pl:x",
               "dry": False, "delay": 0}, fh)
try:
    SP.run_from_cmd(_cmd2)
    with open(SP.STATUS, encoding="utf-8") as fh:
        full = json.load(fh)
    results = full.get("results") or []
    check("ronda gids: se teclea en el orden pedido",
          _calls == ["Grupo Tres", "Grupo Once", "Grupo Dos"], _calls)
    check("ronda gids: results conserva el orden con el gid desconocido",
          [r.get("gid") for r in results] == ["333", "999", "111", "222"],
          [r.get("gid") for r in results])
    check("ronda gids: estados por evidencia",
          [r.get("status") for r in results] == ["ok", "unknown", "pending", "unknown"],
          [r.get("status") for r in results])
    check("ronda gids: gid desconocido no toca el navegador",
          results[1].get("reason") == "gid desconocido", results[1])
    check("ronda gids: permalink de otro gid => gid no coincide",
          results[3].get("reason") == "gid no coincide" and results[3].get("ok") is False,
          results[3])
    check("ronda gids: proof se propaga al status",
          results[0].get("proof") == "/groups/333/posts/1/", results[0])
    check("ronda gids: conteos unknown/pending",
          full.get("unknown") == 2 and full.get("pending") == 1, full)
    check("ronda gids: published solo cuenta oks reales",
          full.get("published") == 1, full.get("published"))
    check("ronda gids: total incluye los saltados", full.get("total") == 4, full.get("total"))
    check("ronda gids: post_key propagado", full.get("post_key") == "pl:x", full)
    check("ronda gids: status cerrado", full.get("running") is False
          and full.get("finished_at", 0) > 0, full)
    check("ronda gids: campos historicos intactos",
          all(k in full for k in ("running", "dry", "total", "done", "published",
                                  "current", "results", "started_at", "finished_at")),
          sorted(full))
finally:
    SP.cxtlib.ensure_background_tab = _orig_ensure
    SP.shared.reconcile_approvals = _orig_reconcile
    SP.close_dialog = _orig_close
    SP.sleep = _orig_sleep
    SP.share_url_group = _orig_share
    SP.ledger.lock_browser = _orig_lock
    SP.load_groups = _orig_load2
    SP.STATUS = _orig_status2
    if _env_before is None:
        os.environ.pop("CXT_LOCK_HELD", None)
    else:
        os.environ["CXT_LOCK_HELD"] = _env_before

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
