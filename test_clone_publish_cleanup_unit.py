import ast
import unittest
from pathlib import Path
from unittest import mock


def load_cleanup_functions():
    source = Path(__file__).with_name("clone_publish.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"cxt_ui", "clear_drafts"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {
        "cdp": mock.Mock(),
        "call": mock.Mock(),
        "log": mock.Mock(),
        "CXT_UI_ON": "ui-on",
        "CXT_UI_OFF": "ui-off",
        "TAB": 1,
    }
    exec(compile(module, "clone_publish.py", "exec"), namespace)
    return namespace


class ClonePublishCleanupTests(unittest.TestCase):
    def test_clear_drafts_success(self):
        ns = load_cleanup_functions()
        ns["call"] = mock.Mock()
        ns["cdp"] = mock.Mock(return_value=2)
        ns["log"] = mock.Mock()

        self.assertTrue(ns["clear_drafts"]())
        ns["call"].assert_called_once()
        ns["cdp"].assert_called_once()

    def test_clear_drafts_failure_logs_and_returns_false(self):
        ns = load_cleanup_functions()
        ns["call"] = mock.Mock()
        ns["cdp"] = mock.Mock(side_effect=RuntimeError("cdp down"))
        ns["log"] = mock.Mock()

        self.assertFalse(ns["clear_drafts"]())
        ns["log"].assert_called()

    def test_cxt_ui_success(self):
        ns = load_cleanup_functions()
        ns["cdp"] = mock.Mock()
        ns["log"] = mock.Mock()

        self.assertTrue(ns["cxt_ui"](True))

    def test_cxt_ui_failure_logs_and_returns_false(self):
        ns = load_cleanup_functions()
        ns["cdp"] = mock.Mock(side_effect=RuntimeError("ui down"))
        ns["log"] = mock.Mock()

        self.assertFalse(ns["cxt_ui"](False))
        ns["log"].assert_called()


if __name__ == "__main__":
    unittest.main()
