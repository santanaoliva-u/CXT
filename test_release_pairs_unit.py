#!/usr/bin/env python3
"""Test unitario de release_pairs.py (sin navegador, sin daemon, tempdir).

Lo que se protege aqui es la regla anti-duplicado: una pareja `unknown` solo se
libera si la traza de esa ronda NO tiene un `submit`/`confirm_post` con ok.

Uso: python3 test_release_pairs_unit.py   (exit 0 = PASS)
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="release_pairs_unit_")
os.environ["CXT_DIR"] = _TMP

import ledger as L  # noqa: E402
import release_pairs as R  # noqa: E402

fails = []
W = 20 * 60 * 1000


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


try:
    # 1. veredicto a partir de la traza -------------------------------------
    t = 1_700_000_000_000
    check("veredicto: submit ok bloquea la liberacion",
          R.verdict([{"ts": t, "step": "submit", "ok": True}], t, W)[0] == "no-liberar")
    check("veredicto: confirm_post ok bloquea la liberacion",
          R.verdict([{"ts": t, "step": "confirm_post", "ok": True}], t, W)[0] == "no-liberar")
    check("veredicto: fallo antes del envio libera",
          R.verdict([{"ts": t, "step": "search_select", "ok": False}], t, W)[0] == "liberable")
    check("veredicto: submit fallido sin otro fallo NO libera",
          R.verdict([{"ts": t, "step": "submit", "ok": False}], t, W)[0] == "no-liberar")
    check("veredicto: traza vacia NO libera", R.verdict([], t, W)[0] == "no-liberar")
    check("veredicto: sin fichero de traza NO libera", R.verdict(None, t, W)[0] == "no-liberar")
    check("veredicto: fuera de la ventana NO libera",
          R.verdict([{"ts": t + 3 * W, "step": "open_share", "ok": False}], t, W)[0] == "no-liberar")

    # 2. CLI sobre un ledger temporal --------------------------------------
    DB = os.path.join(_TMP, "rel.db")
    TR = os.path.join(_TMP, "trace.jsonl")
    L.init(DB)
    L.result("111", "pk1", "unknown", False, ts=t, db=DB)
    L.result("222", "pk1", "unknown", False, ts=t, db=DB)
    with open(TR, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": t, "group": "uno", "step": "search_select", "ok": False}) + "\n")
        fh.write(json.dumps({"ts": t, "group": "dos", "step": "submit", "ok": True}) + "\n")
    L.group_state("111", name="uno", db=DB)
    L.group_state("222", name="dos", db=DB)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = R.main(["--db", DB, "--trace", TR, "--json"])
    pairs = {p["gid"]: p for p in json.loads(buf.getvalue())}
    check("simulacro: no escribe", rc == 0
          and L.post_group_map(DB)[("111", "pk1")]["status"] == "unknown")
    check("cli: marca liberable la pareja sin submit",
          pairs["111"]["veredicto"] == "liberable", pairs.get("111"))
    check("cli: marca no-liberar la pareja con submit ok",
          pairs["222"]["veredicto"] == "no-liberar", pairs.get("222"))
    check("cli: --apply sin --reason -> 2", R.main(["--db", DB, "--trace", TR, "--apply"]) == 2)
    with contextlib.redirect_stdout(io.StringIO()):
        R.main(["--db", DB, "--trace", TR, "--apply", "--reason", "ronda sin click"])
    after = L.post_group_map(DB)
    check("cli: --apply libera solo la segura",
          after[("111", "pk1")]["status"] == "absent" and after[("222", "pk1")]["status"] == "unknown",
          after)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
if not fails:
    print("PASS")
sys.exit(0 if not fails else 1)
