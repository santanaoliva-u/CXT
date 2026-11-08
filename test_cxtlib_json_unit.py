#!/usr/bin/env python3
"""Robustez de estado JSON: fail-open ruidoso + durabilidad (A-ROB-6)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import cxtlib
import scheduler


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "state.json")

    def tearDown(self):
        self._tmp.cleanup()


class LoadJsonTest(TempCase):
    def test_missing_returns_default(self):
        self.assertEqual(cxtlib.load_json(self.path, {"a": 1}), {"a": 1})
        self.assertEqual(cxtlib.load_json(self.path), {})

    def test_missing_strict_still_returns_default(self):
        self.assertEqual(cxtlib.load_json(self.path, "x", strict=True), "x")

    def test_valid_json_is_read(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"ok": True}, f)
        self.assertEqual(cxtlib.load_json(self.path, {}), {"ok": True})

    def test_corrupt_strict_raises_with_path(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{no es json")
        with self.assertRaises(cxtlib.JsonStateError) as ctx:
            cxtlib.load_json(self.path, {}, strict=True)
        self.assertIn(self.path, str(ctx.exception))

    def test_corrupt_non_strict_warns_and_returns_default(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{no es json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = cxtlib.load_json(self.path, {"d": 1})
        self.assertEqual(res, {"d": 1})
        self.assertIn("load_json warn", out.getvalue())

    def test_unreadable_file_is_not_silent_in_strict(self):
        # directorio en vez de archivo => OSError (IsADirectoryError)
        with self.assertRaises(cxtlib.JsonStateError):
            cxtlib.load_json(self.dir, {}, strict=True)


class WriteJsonAtomicTest(TempCase):
    def test_writes_content_and_no_tmp_left(self):
        cxtlib.write_json_atomic(self.path, {"a": [1, 2]}, indent=2)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": [1, 2]})
        for name in os.listdir(self.dir):
            self.assertFalse(name.endswith(".tmp"), name)

    def test_fsync_file_and_dir_before_finishing(self):
        events = []
        real_replace = os.replace

        def fake_fsync(fd):
            events.append("fsync")

        def fake_replace(a, b):
            events.append("replace")
            return real_replace(a, b)

        with mock.patch("cxtlib.os.fsync", side_effect=fake_fsync), \
                mock.patch("cxtlib.os.replace", side_effect=fake_replace):
            cxtlib.write_json_atomic(self.path, {"ok": 1})
        self.assertGreaterEqual(events.count("fsync"), 2, events)
        self.assertIn("replace", events)
        self.assertLess(events.index("fsync"), events.index("replace"), events)

    def test_fsync_failure_propagates_and_removes_tmp(self):
        with mock.patch("cxtlib.os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                cxtlib.write_json_atomic(self.path, {"ok": 1})
        self.assertEqual(os.listdir(self.dir), [])

    def test_dir_open_failure_is_best_effort(self):
        with mock.patch("cxtlib.os.open", side_effect=OSError("no dir")):
            cxtlib.write_json_atomic(self.path, {"ok": 1})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"ok": 1})


class SchedulerJsonTest(TempCase):
    def test_read_json_corrupt_strict_raises(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("nope")
        with self.assertRaises(cxtlib.JsonStateError):
            scheduler.read_json(self.path, {}, strict=True)

    def test_read_json_corrupt_non_strict_warns(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("nope")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(scheduler.read_json(self.path, {"d": 1}), {"d": 1})
        self.assertIn("read_json warn", out.getvalue())

    def test_read_json_missing_returns_default(self):
        self.assertEqual(scheduler.read_json(self.path, {"d": 1}), {"d": 1})

    def test_write_json_is_durable_and_clean(self):
        calls = []
        real_replace = os.replace
        with mock.patch("cxtlib.os.fsync", side_effect=lambda fd: calls.append("fsync")), \
                mock.patch("cxtlib.os.replace", side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))):
            scheduler.write_json(self.path, {"a": 1})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertGreaterEqual(calls.count("fsync"), 2, calls)
        self.assertEqual(os.listdir(self.dir), ["state.json"])


if __name__ == "__main__":
    unittest.main()
