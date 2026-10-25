#!/usr/bin/env python3
"""E2E dry (SIN navegador, SIN daemon, SIN red) del envio en modo rapido.

Simula el envio con un `cxt` falso que registra operaciones:
  - `shared.submit` en dry NO pulsa Publicar (`clicks_de_publicar == 0`).
  - `verify_feed` NUNCA se llama en FAST (sin permalink no hay prueba fuerte).
  - modo normal (control negativo) SI llama `verify_feed` una vez.

Hermetico: CXT_DIR temporal ANTES de importar; `cxtlib.call` se sustituye por
un grabador en memoria, asi que no hay subproceso, red ni escritura en ~/.cxt.
Uso: LC_ALL=C python3 test_fast_e2e_dry.py   (exit 0 = PASS)
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

os.environ["CXT_DIR"] = tempfile.mkdtemp(prefix="fast_e2e_dry_")
os.environ.pop("CXT_FAST", None)

import fastmode  # noqa: E402
import autopost as AP  # noqa: E402
import shared as SH  # noqa: E402

_CLICK_OPS = ("cdpclick", "click", "cdpkey")


class FakeCxt:
    """Objeto `cxt` falso: registra cada operacion en `self.ops`."""

    def __init__(self):
        self.ops = []

    def record(self, op, args):
        self.ops.append({"op": op, "args": args or {}})

    def clicks_de_publicar(self):
        return len([o for o in self.ops if o["op"] in _CLICK_OPS])

    def expr(self, i):
        return ((self.ops[i]["args"].get("params") or {}).get("expression") or "")


def fake_submit_rect():
    return {"result": {"result": {"value": {"x": 100, "y": 200,
                                            "label": "Publicar"}}}}


class DrySubmitE2E(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("CXT_FAST")
        os.environ.pop("CXT_FAST", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CXT_FAST", None)
        else:
            os.environ["CXT_FAST"] = self._saved

    def _recorder(self, fake):
        def call(cxt, op, args=None, timeout=120):
            cxt.record(op, args)
            if op == "cdp":
                return fake_submit_rect()
            if op == "tabs":
                return [{"id": 1, "url": "https://www.facebook.com/groups/111"}]
            return {}
        return call

    def test_dry_submit_fast_zero_clicks_zero_verify_feed(self):
        fake = FakeCxt()
        vf = {"n": 0}

        def fake_verify_feed(*a, **k):
            vf["n"] += 1
            return {"ok": False, "reason": "sin-prueba", "count": 0}

        os.environ["CXT_FAST"] = "1"
        with mock.patch.object(SH.cxtlib, "call", side_effect=self._recorder(fake)), \
             mock.patch.object(SH, "verify_feed", side_effect=fake_verify_feed):
            r = SH.submit(fake, 1, True, text="hola",
                          verify={"gid": "111", "fbid": "", "before": 0,
                                  "needle": "hola", "author": "", "group": "G"})

        self.assertTrue(r.get("ok"))
        self.assertIs(r.get("dry"), True)
        self.assertEqual(r.get("reason"), "dry")
        self.assertEqual(fake.clicks_de_publicar(), 0)
        self.assertEqual(vf["n"], 0)
        self.assertNotIn("cdpclick", [o["op"] for o in fake.ops])
        # el payload/mode del modo rapido
        self.assertEqual(fastmode.tag(), "fast")
        payload = AP.share_cmd_payload({"fast": True, "url": "u"}, ["111"], "k")
        self.assertIs(payload["fast"], True)
        self.assertEqual(payload["delay"], 1.5)


class FastVerifyFeedDifferential(unittest.TestCase):
    """FAST omite verify_feed; el modo normal lo llama (control negativo)."""

    def setUp(self):
        self._saved = os.environ.get("CXT_FAST")
        os.environ.pop("CXT_FAST", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CXT_FAST", None)
        else:
            os.environ["CXT_FAST"] = self._saved

    def _run_submit(self):
        fake = FakeCxt()
        vf = {"n": 0}

        def rec(cxt, op, args=None, timeout=120):
            cxt.record(op, args)
            if op == "cdp":
                expr = ((args or {}).get("params") or {}).get("expression") or ""
                if expr == SH.JS_HIT:
                    return {"result": {"result": {"value": {"x": 10, "y": 10}}}}
                return {"result": {"result": {"value": ""}}}
            return {}

        def fake_verify_feed(*a, **k):
            vf["n"] += 1
            return {"ok": False, "reason": "sin-prueba", "count": 0}

        with mock.patch.object(SH.cxtlib, "call", side_effect=rec), \
             mock.patch.object(SH, "_wait_submit",
                               return_value=({"x": 10, "y": 10, "label": "Publicar"}, {})), \
             mock.patch.object(SH, "_isolate_submit",
                               return_value={"x": 10, "y": 10, "label": "Publicar"}), \
             mock.patch.object(SH, "_visible_pids", return_value=set()), \
             mock.patch.object(SH, "poll_js_settle", return_value=(True, {})), \
             mock.patch.object(SH, "confirm_new_post_permalink",
                               return_value={"ok": False, "reason": "sin-prueba"}), \
             mock.patch.object(SH, "verify_feed", side_effect=fake_verify_feed):
            r = SH.submit(fake, 1, False, text="x",
                          verify={"gid": "111", "fbid": "", "before": 0,
                                  "needle": "x", "author": "", "group": "G"})
        return r, fake, vf

    def test_fast_skips_verify_feed(self):
        os.environ["CXT_FAST"] = "1"
        r, fake, vf = self._run_submit()
        self.assertEqual(vf["n"], 0)
        self.assertEqual(fake.clicks_de_publicar(), 1)  # si pulso Publicar
        self.assertFalse(r.get("ok"))
        self.assertEqual(r.get("reason"), "publicado-sin-confirmar")

    def test_normal_calls_verify_feed(self):
        r, fake, vf = self._run_submit()
        self.assertEqual(vf["n"], 1)
        self.assertEqual(fake.clicks_de_publicar(), 1)
        self.assertFalse(r.get("ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
