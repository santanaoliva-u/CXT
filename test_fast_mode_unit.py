#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon, SIN red) del modo rapido FAST.

Cubre: activacion de `fastmode.enabled`, perfil numerico `knobs()`, etiqueta
`tag()`, `share_cmd_payload`, `effective_interval`, las restricciones duras de
`_group_window` (el FAST salta cooldown/gap pero NO topes diarios ni terminales
de pareja) y la simulacion de checkpoint/bloqueo -> halt sin publicar nada.

Hermetico: CXT_DIR apunta a un directorio temporal ANTES de importar autopost,
asi none de las rutas de estado reales (~/.cxt/*) se toca.

Uso: LC_ALL=C python3 test_fast_mode_unit.py   (exit 0 = PASS)
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Hermetismo: rutas de estado a un temp ANTES de importar los modulos.
_TMP = tempfile.mkdtemp(prefix="fastmode_unit_")
os.environ["CXT_DIR"] = _TMP
os.environ.pop("CXT_FAST", None)

import fastmode  # noqa: E402
import autopost as AP  # noqa: E402
import ledger as L  # noqa: E402


class FastModeTest(unittest.TestCase):
    def setUp(self):
        # No filtrar CXT_FAST entre tests.
        self._saved = os.environ.get("CXT_FAST")
        os.environ.pop("CXT_FAST", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CXT_FAST", None)
        else:
            os.environ["CXT_FAST"] = self._saved

    # 1. activacion -------------------------------------------------------
    def test_enabled_env_truthy(self):
        for v in ("1", "true", "yes", "si", "on"):
            self.assertTrue(fastmode.enabled(env={"CXT_FAST": v}), v)
            self.assertTrue(fastmode.enabled(env={"CXT_FAST": v.upper()}), v)

    def test_enabled_env_falsy_or_missing(self):
        for v in ("0", "", "no", "off"):
            self.assertFalse(fastmode.enabled(env={"CXT_FAST": v}), v)
        self.assertFalse(fastmode.enabled(env={}))

    def test_enabled_item_flag(self):
        self.assertTrue(fastmode.enabled(env={}, item={"fast": True}))
        self.assertFalse(fastmode.enabled(env={}, item={"fast": False}))

    # 2. perfil exacto ----------------------------------------------------
    def test_knobs_exact(self):
        self.assertEqual(fastmode.knobs(), {
            "inter_group_delay_s": 1.5, "jitter": 0.0, "cooldown_h": 0.0,
            "gap_h": 0.0, "verify_feed": False, "permalink_budget_s": 5.0,
            "max_attempts": 1, "min_interval_min": 1.0})

    def test_knobs_is_copy(self):
        k = fastmode.knobs()
        k["inter_group_delay_s"] = 999
        self.assertEqual(fastmode.knobs()["inter_group_delay_s"], 1.5)

    # 3. tag / verify_feed_enabled ---------------------------------------
    def test_tag(self):
        self.assertEqual(fastmode.tag(env={"CXT_FAST": "1"}), "fast")
        self.assertEqual(fastmode.tag(env={}), "normal")
        self.assertEqual(fastmode.tag(env={}, item={"fast": True}), "fast")

    def test_verify_feed_enabled(self):
        os.environ["CXT_FAST"] = "1"
        self.assertFalse(fastmode.verify_feed_enabled())
        os.environ.pop("CXT_FAST", None)
        self.assertTrue(fastmode.verify_feed_enabled())

    # 4. share_cmd_payload ------------------------------------------------
    def test_share_cmd_payload_fast(self):
        payload = AP.share_cmd_payload({"fast": True, "url": "u"}, ["111"], "k")
        self.assertIs(payload["fast"], True)
        self.assertEqual(payload["delay"], 1.5)

    def test_share_cmd_payload_fast_by_env(self):
        os.environ["CXT_FAST"] = "1"
        payload = AP.share_cmd_payload({"url": "u"}, ["111"], "k")
        self.assertEqual(payload["delay"], 1.5)

    def test_share_cmd_payload_normal(self):
        payload = AP.share_cmd_payload({"url": "u"}, ["111"], "k")
        self.assertIs(payload["fast"], False)
        self.assertNotEqual(payload["delay"], 1.5)
        self.assertGreaterEqual(payload["delay"], 240)
        self.assertLessEqual(payload["delay"], 360)

    # 5. effective_interval ----------------------------------------------
    def test_effective_interval_fast(self):
        vals = [AP.effective_interval({"fast": True}, {}) for _ in range(20)]
        self.assertTrue(all(v == 1.0 for v in vals), vals)
        # override explicito no baja del suelo FAST.
        self.assertEqual(AP.effective_interval({"fast": True},
                                               {"interval_override": 0.2}), 1.0)

    def test_effective_interval_normal_unchanged(self):
        self.assertEqual(AP.effective_interval({}, {}), AP.INTERVAL_REC)
        self.assertEqual(AP.effective_interval({"interval_min": 25}, {}), 25.0)
        self.assertEqual(AP.effective_interval({}, {"interval_min": 30}), 30.0)

    # 6. restricciones duras de _group_window -----------------------------
    def _rel(self):
        return AP.load_relevance()

    def test_group_window_fast_skips_gap_and_cooldown(self):
        now = 1_700_000_000_000
        rel = self._rel()
        gid, pk = "111", "pl:x"
        group = {"name": "Bazar Vecinos", "status": "ok", "day_n": 0,
                 "day_utc": L.utc_day(now), "last_any": now - 3_600_000}
        # gap de grupo de 6 h: normal bloquea, FAST salta.
        self.assertFalse(AP._group_window(gid, group, {}, pk, now, rel, fast=False)[0])
        self.assertTrue(AP._group_window(gid, group, {}, pk, now, rel, fast=True)[0])
        # cooldown de pareja (last_any 0 para aislar el cooldown): normal bloquea.
        group2 = dict(group, last_any=0)
        pm = {(gid, pk): {"last_ts": now - 3_600_000, "status": "ok"}}
        self.assertEqual(AP._group_window(gid, group2, pm, pk, now, rel, fast=False)[1],
                         "cooldown_post")
        self.assertTrue(AP._group_window(gid, group2, pm, pk, now, rel, fast=True)[0])

    def test_group_window_fast_keeps_daily_cap(self):
        now = 1_700_000_000_000
        rel = self._rel()
        group = {"name": "Bazar Vecinos", "status": "ok", "day_n": AP.MAX_GRUPO_DIA,
                 "day_utc": L.utc_day(now), "last_any": 0}
        for fast in (False, True):
            ok, why, _ = AP._group_window("111", group, {}, "pl:x", now, rel, fast=fast)
            self.assertFalse(ok)
            self.assertEqual(why, "cupo_diario")

    def test_group_window_fast_keeps_pair_terminal(self):
        now = 1_700_000_000_000
        rel = self._rel()
        group = {"name": "Bazar Vecinos", "status": "ok", "day_n": 0,
                 "day_utc": L.utc_day(now), "last_any": 0}
        for st in ("unknown", "pending", "blocked", "removed"):
            pm = {("111", "pl:x"): {"last_ts": 0, "status": st}}
            for fast in (False, True):
                ok, why, _ = AP._group_window("111", group, pm, "pl:x", now, rel, fast=fast)
                self.assertFalse(ok, st)
                self.assertEqual(why, "pair:" + st)

    def test_terminal_attempt_not_retried(self):
        self.assertTrue(AP._attempt_terminal({"published": 1}))
        self.assertTrue(AP._attempt_terminal({"state": "hecho"}))
        self.assertFalse(AP._retryable({"dry": False}, {"published": 1}))
        self.assertFalse(AP._retryable({"dry": False}, {"unresolved": 1}))
        self.assertFalse(AP._retryable({"dry": False}, {"pending": 1}))
        self.assertTrue(AP._retryable({"dry": False}, {"tries": 0}))

    # 7. checkpoint / halt: no se publica --------------------------------
    def _write_queue(self):
        import json
        now = int(time.time() * 1000)
        q = {"paused": False, "items": [{
            "url": "https://www.facebook.com/groups/111/posts/222/",
            "expect": "hola", "state": "pendiente", "dry": True, "at": now}]}
        with open(AP.QUEUE, "w", encoding="utf-8") as fh:
            json.dump(q, fh)

    def _run_tick(self, checkpoint):
        self._write_queue()
        publish = {"n": 0}

        def fake_publish(*a, **k):
            publish["n"] += 1

        st = AP.default_status()
        with mock.patch.object(AP, "health",
                               return_value={"enabled": True, "share_alive": False,
                                             "share_post_alive": False}), \
             mock.patch.object(AP, "_dedicated_tab", return_value=1), \
             mock.patch.object(AP, "checkpoint_now", return_value=checkpoint), \
             mock.patch.object(AP, "_publish", side_effect=fake_publish):
            AP._tick(st, os.path.join(_TMP, "no-existe.cmd.json"))
        return st, publish["n"]

    def test_block_alerts_and_no_publish(self):
        try:
            os.remove(AP.HALT)
        except OSError:
            pass
        st, clicks = self._run_tick("block")
        self.assertFalse(st.get("pausado"), st.get("error"))
        self.assertFalse(os.path.exists(AP.HALT))
        self.assertTrue(st.get("alerta"), st)
        self.assertTrue(st.get("error", "").startswith("ALERTA:"), st.get("error"))
        self.assertEqual(clicks, 0)

    def test_checkpoint_url_alerts_and_no_publish(self):
        try:
            os.remove(AP.HALT)
        except OSError:
            pass
        st, clicks = self._run_tick("https://www.facebook.com/checkpoint/?x=1")
        self.assertFalse(st.get("pausado"), st.get("error"))
        self.assertTrue(st.get("alerta"), st)
        self.assertEqual(st["alerta"].get("reason"), "cuenta restringida")
        self.assertEqual(clicks, 0)
        self.assertFalse(os.path.exists(AP.HALT))

    # 8. dry: no toca el estado real -------------------------------------
    def test_tempdir_isolation(self):
        self.assertEqual(AP.DIR, _TMP)
        self.assertTrue(AP.QUEUE.startswith(_TMP))


if __name__ == "__main__":
    unittest.main(verbosity=2)
