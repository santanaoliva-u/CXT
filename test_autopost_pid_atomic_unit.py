import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_write_pid():
    source = Path(__file__).with_name("autopost.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_write_pid"
    )
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {"os": os}
    exec(compile(module, "autopost.py", "exec"), namespace)
    return namespace


class AutopostPidAtomicTests(unittest.TestCase):
    def test_write_pid_replaces_atomically_with_0600(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "autopost.pid"
            pid_path.write_text("old-pid", encoding="utf-8")
            pid_path.chmod(0o644)

            ns = load_write_pid()
            ns["DIR"] = tmpdir
            ns["PID"] = str(pid_path)
            ns["_write_pid"]()

            self.assertEqual(pid_path.read_text(encoding="utf-8"), str(os.getpid()))
            self.assertEqual(pid_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmpdir).glob(".autopost.pid.*.tmp")), [])

    def test_write_pid_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "autopost.pid"
            pid_path.write_text("old-pid", encoding="utf-8")

            ns = load_write_pid()
            ns["DIR"] = tmpdir
            ns["PID"] = str(pid_path)

            with mock.patch("os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    ns["_write_pid"]()

            self.assertEqual(pid_path.read_text(encoding="utf-8"), "old-pid")
            self.assertEqual(list(Path(tmpdir).glob(".autopost.pid.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
