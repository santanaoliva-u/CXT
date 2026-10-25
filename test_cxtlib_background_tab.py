#!/usr/bin/env python3
"""Tests unitarios (sin navegador ni daemon) del contrato sin foco de cxtlib."""
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cxtlib  # noqa: E402


class EnsureBackgroundTabTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.call = mock.patch.object(cxtlib, "call").start()
        self.addCleanup(mock.patch.stopall)

    def _ops(self):
        return [c.args[1] for c in self.call.call_args_list]

    def test_returns_tab_id_and_uses_ensure_auto_when_available(self):
        self.call.return_value = {"tabId": 42, "windowId": 7, "created": True,
                                  "focused": False}
        tab = cxtlib.ensure_background_tab("bin/cxt")
        self.assertEqual(tab, 42)
        self.assertEqual(self._ops(), ["tab.ensure_auto"])
        self.assertEqual(self.call.call_args.args[2],
                         {"url": "https://www.facebook.com/"})

    def test_passes_explicit_url(self):
        self.call.return_value = {"tabId": 8}
        cxtlib.ensure_background_tab("bin/cxt", "https://www.facebook.com/groups/x")
        self.assertEqual(self.call.call_args.args[2],
                         {"url": "https://www.facebook.com/groups/x"})

    def test_old_extension_raises_and_never_activates(self):
        self.call.return_value = {"error": "op desconocida en pagina: tab.ensure_auto"}
        with self.assertRaises(RuntimeError) as ctx:
            cxtlib.ensure_background_tab("bin/cxt")
        self.assertIn("tab.ensure_auto", str(ctx.exception))
        self.assertNotIn("tab.activate", self._ops())


class EnsureTabNoFocusTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.call = mock.patch.object(cxtlib, "call").start()
        self.addCleanup(mock.patch.stopall)

    def test_default_resolves_without_activating(self):
        tab = cxtlib.ensure_tab("bin/cxt", tab=5)
        self.assertEqual(tab, 5)
        self.assertEqual(self.call.call_args_list, [])

    def test_activate_flag_is_explicit_only(self):
        tab = cxtlib.ensure_tab("bin/cxt", tab=5, activate=True)
        self.assertEqual(tab, 5)
        self.assertEqual(self.call.call_args.args[1], "tab.activate")

    def test_marked_tab_is_preferred_over_first_facebook_tab(self):
        self.call.return_value = [
            {"id": 1, "url": "https://www.facebook.com/"},
            {"id": 2, "url": "https://www.facebook.com/groups/#cxt-auto"},
        ]
        self.assertEqual(cxtlib.resolve_tab("bin/cxt"), 2)

    def test_cxt_tab_still_wins(self):
        os.environ["CXT_TAB"] = "9"
        self.assertEqual(cxtlib.resolve_tab("bin/cxt"), 9)
        self.assertEqual(self.call.call_args_list, [])


if __name__ == "__main__":
    unittest.main()
