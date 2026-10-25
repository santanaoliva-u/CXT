#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon) de la ruta `sin-prueba`.

Cubre los dos fallos raiz que producian `sin-prueba` y el falso positivo:
  - `shared.pick_new_post_permalink` con candidatos `ts=0` (fail-closed salvo
    pid NUEVO respecto a la linea base) y normalizacion de epoch en ms.
  - `autopost.clean_expect`: recorte de sufijos de UI, colapso de espacios,
    tope de 160 y caso None/vacio.
  - `shared.classify_send_verdict`: una prueba debil sin gid JAMAS concede ok.

Hermetico: CXT_DIR temporal ANTES de importar.
Uso: LC_ALL=C python3 test_sin_prueba_unit.py   (exit 0 = PASS)
"""
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_TMP = tempfile.mkdtemp(prefix="sinprueba_unit_")
os.environ["CXT_DIR"] = _TMP
os.environ.pop("CXT_FAST", None)

import shared as SH  # noqa: E402
import autopost as AP  # noqa: E402


GID = "111"
PID = "222"


def cand(ts=0, text="hola bazar", fbid=""):
    return {"url": "https://www.facebook.com/groups/%s/posts/%s/" % (GID, PID),
            "gid": GID, "pid": PID, "ts": ts, "text": text, "fbid": fbid}


class PickPermalinkTest(unittest.TestCase):
    # 1. ts=0 + baseline SIN el pid + identidad -> acepta ------------------
    def test_ts0_new_pid_identity_accepted(self):
        got = SH.pick_new_post_permalink([cand(ts=0)], GID, needle="hola bazar",
                                         before_pids=["999"])
        self.assertIsNotNone(got)
        self.assertEqual(got["pid"], PID)

    def test_ts0_new_pid_fbid_identity_accepted(self):
        got = SH.pick_new_post_permalink([cand(ts=0, text="x", fbid="F1")], GID,
                                         fbid="F1", before_pids=["999"])
        self.assertIsNotNone(got)

    # 2. ts=0 + baseline CON el pid -> rechaza (no es nuevo) ---------------
    def test_ts0_pid_already_visible_rejected(self):
        got = SH.pick_new_post_permalink([cand(ts=0)], GID, needle="hola bazar",
                                         before_pids=[PID])
        self.assertIsNone(got)

    # 3. ts=0 + before_pids=None -> rechaza (fail-closed) ------------------
    def test_ts0_no_baseline_fail_closed(self):
        got = SH.pick_new_post_permalink([cand(ts=0)], GID, needle="hola bazar",
                                         before_pids=None)
        self.assertIsNone(got)

    # 4. ts=0 + baseline sin pid pero identidad distinta -> rechaza --------
    def test_ts0_identity_mismatch_rejected(self):
        got = SH.pick_new_post_permalink([cand(ts=0, text="otra cosa", fbid="X")],
                                         GID, needle="no coincide", fbid="Y",
                                         before_pids=["999"])
        self.assertIsNone(got)

    # 5/6. ts>0 vs not_before_ts (segundos y ms) ---------------------------
    def test_ts_positive_respects_not_before(self):
        self.assertIsNotNone(SH.pick_new_post_permalink(
            [cand(ts=1_700_000_100)], GID, needle="hola bazar",
            not_before_ts=1_700_000_000))
        self.assertIsNone(SH.pick_new_post_permalink(
            [cand(ts=1_699_999_000)], GID, needle="hola bazar",
            not_before_ts=1_700_000_000))

    def test_ms_timestamps_normalized(self):
        got = SH.pick_new_post_permalink(
            [cand(ts=1_700_000_100_000)], GID, needle="hola bazar",
            not_before_ts=1_700_000_000_000)
        self.assertIsNotNone(got)
        self.assertEqual(got["ts"], 1_700_000_100)  # ms -> s

    def test_ts_positive_without_identity_rejected(self):
        self.assertIsNone(SH.pick_new_post_permalink(
            [cand(ts=1_700_000_100, text="x")], GID, needle="no coincide",
            not_before_ts=0))


class CleanExpectTest(unittest.TestCase):
    def test_suffix_cut_ver_mas(self):
        self.assertEqual(AP.clean_expect("Hola mundo … Ver más"), "Hola mundo")
        self.assertEqual(AP.clean_expect("Hola mundo ... Ver más"), "Hola mundo")
        self.assertEqual(AP.clean_expect("Hola mundo Ver más"), "Hola mundo")

    def test_suffix_cut_ver_traduccion(self):
        self.assertEqual(AP.clean_expect("Hola Ver traducción"), "Hola")

    def test_suffix_cut_hace_aproximad(self):
        self.assertEqual(AP.clean_expect("Post hace aproximad…"), "Post")

    def test_ellipsis_and_whitespace_collapse(self):
        self.assertEqual(AP.clean_expect("Hola...   mundo"), "Hola mundo")
        self.assertEqual(AP.clean_expect("  hola   mundo  "), "hola mundo")

    def test_cap_160(self):
        out = AP.clean_expect("a" * 300)
        self.assertLessEqual(len(out), 160)
        self.assertEqual(len(out), 160)

    def test_none_and_empty(self):
        self.assertEqual(AP.clean_expect(None), "")
        self.assertEqual(AP.clean_expect(""), "")
        self.assertEqual(AP.clean_expect(123), "")

    def test_empty_result_returns_original(self):
        # Todo el texto es sufijo de UI: no se pierde la unica identificacion.
        self.assertEqual(AP.clean_expect("... Ver más"), "... Ver más")


class ClassifyVerdictTest(unittest.TestCase):
    def test_weak_without_gid_not_ok(self):
        ok, reason, pending = SH.classify_send_verdict(weak="composer-cerrado")
        self.assertFalse(ok)
        self.assertEqual(reason, "publicado-sin-confirmar")
        self.assertFalse(pending)

    def test_no_signal_sin_prueba(self):
        ok, reason, _ = SH.classify_send_verdict()
        self.assertFalse(ok)
        self.assertEqual(reason, "sin-prueba")

    def test_strong_proof_is_ok(self):
        proof = "https://www.facebook.com/groups/%s/posts/%s/" % (GID, PID)
        ok, reason, pending = SH.classify_send_verdict(proof=proof,
                                                       weak="composer-cerrado")
        self.assertTrue(ok)
        self.assertEqual(reason, proof)
        self.assertFalse(pending)

    def test_pending_hint_not_ok(self):
        ok, reason, pending = SH.classify_send_verdict(pending_hint="pendiente")
        self.assertFalse(ok)
        self.assertEqual(reason, "pendiente-aprobacion")
        self.assertTrue(pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
