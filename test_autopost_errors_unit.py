#!/usr/bin/env python3
"""Excepciones ya no silenciosas en autopost (health/_tab_list/dialogos)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="autopost_err_unit_")
os.environ["CXT_DIR"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import autopost as AP  # noqa: E402


def _run(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = fn(*args)
    return res, out.getvalue()


class HealthTest(unittest.TestCase):
    def test_health_error_is_logged_and_returns_none(self):
        with mock.patch.object(AP.urllib.request, "urlopen", side_effect=OSError("conn down")):
            res, out = _run(AP.health)
        self.assertIsNone(res)
        self.assertIn("health err", out)
        self.assertIn("conn down", out)

    def test_health_ok_is_not_logged(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        with mock.patch.object(AP.urllib.request, "urlopen", return_value=_Resp()):
            res, out = _run(AP.health)
        self.assertEqual(res, {"ok": True})
        self.assertNotIn("health err", out)


class TabListTest(unittest.TestCase):
    def test_tab_list_error_is_logged_and_returns_empty(self):
        with mock.patch.object(AP.cxtlib, "call", side_effect=RuntimeError("no cxt")):
            res, out = _run(AP._tab_list)
        self.assertEqual(res, [])
        self.assertIn("tab_list err", out)
        self.assertIn("no cxt", out)

    def test_tab_list_ok(self):
        with mock.patch.object(AP.cxtlib, "call", return_value={"tabs": [{"url": "u"}]}):
            res, out = _run(AP._tab_list)
        self.assertEqual(res, [{"url": "u"}])
        self.assertNotIn("tab_list err", out)


class DialogsTest(unittest.TestCase):
    def test_warning_dialog_error_is_logged(self):
        with mock.patch.object(AP.cxtlib, "cdp", side_effect=RuntimeError("cdp roto")):
            res, out = _run(AP.warning_dialog, 7)
        self.assertFalse(res)
        self.assertIn("warning_dialog err", out)
        self.assertIn("cdp roto", out)

    def test_block_notice_error_is_logged(self):
        with mock.patch.object(AP.cxtlib, "cdp", side_effect=RuntimeError("cdp roto")):
            res, out = _run(AP.block_notice, 7)
        self.assertFalse(res)
        self.assertIn("block_notice err", out)

    def test_dialog_hit_is_quiet(self):
        with mock.patch.object(AP.cxtlib, "cdp", return_value=True):
            res, out = _run(AP.block_notice, 7)
        self.assertTrue(res)
        self.assertNotIn("block_notice err", out)

    def test_none_tab_short_circuits(self):
        with mock.patch.object(AP.cxtlib, "cdp", side_effect=AssertionError("no debe llamarse")):
            self.assertFalse(AP.warning_dialog(None))
            self.assertFalse(AP.block_notice(None))


if __name__ == "__main__":
    unittest.main()
