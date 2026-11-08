import ast
import json
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


def load_health():
    source = Path(__file__).with_name("autopost.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "health"
    )
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {
        "PORT": 8799,
        "_token": lambda: "tok",
        "urllib": urllib,
        "json": json,
        "log": lambda msg: None,
    }
    exec(compile(module, "autopost.py", "exec"), namespace)
    return namespace


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok": true}'


class AutopostHealthTokenTests(unittest.TestCase):
    def test_uses_header_and_not_query(self):
        ns = load_health()
        captured = {}

        def fake_urlopen(request, timeout=5):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(ns["health"](), {"ok": True})

        request = captured["request"]
        self.assertEqual(request.full_url, "http://127.0.0.1:8799/health")
        self.assertNotIn("?token=", request.full_url)
        self.assertEqual(request.get_header("X-cxt-token"), "tok")
        self.assertEqual(captured["timeout"], 5)

    def test_no_token_no_header(self):
        ns = load_health()
        ns["_token"] = lambda: ""
        captured = {}

        def fake_urlopen(request, timeout=5):
            captured["request"] = request
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(ns["health"](), {"ok": True})

        self.assertIsNone(captured["request"].get_header("X-cxt-token"))

    def test_network_failure_returns_none(self):
        ns = load_health()
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("down")):
            self.assertIsNone(ns["health"]())


if __name__ == "__main__":
    unittest.main()
