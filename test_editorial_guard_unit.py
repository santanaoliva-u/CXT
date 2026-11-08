"""Regresion A-ROB-8: el historial del sello no puede fallar abierto."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import editorial


def _make_db(path: Path) -> None:
    conn = editorial._connect(path)
    try:
        for i in range(2):
            conn.execute(
                "INSERT INTO runs (id, created_at, instruction, mode, assignment_json,"
                " dossier_json, angles_json, draft_text) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "run%d" % i,
                    "2026-10-26T00:0%d:00" % i,
                    "instr",
                    "draft",
                    "{}",
                    "{}",
                    "[]",
                    "texto %d" % i,
                ),
            )
        conn.commit()
    finally:
        conn.close()


class PreviousPostsGuardTest(unittest.TestCase):
    def test_missing_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(editorial._previous_posts(Path(tmp) / "editorial.db"), [])

    def test_reads_draft_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            _make_db(db)
            posts = editorial._previous_posts(db)
            self.assertEqual(len(posts), 2)
            self.assertIn("texto 1", posts)

    def test_excludes_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            _make_db(db)
            posts = editorial._previous_posts(db, exclude_run_id="run1")
            self.assertEqual(posts, ["texto 0"])

    def test_fails_closed_after_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            db.write_text("no es sqlite", encoding="utf-8")
            calls = []

            def broken(_path):
                calls.append(1)
                raise sqlite3.OperationalError("database is locked")

            with mock.patch.object(editorial, "_connect", side_effect=broken):
                with self.assertRaises(RuntimeError) as ctx:
                    editorial._previous_posts(db, retries=3, retry_delay=0)
            self.assertEqual(len(calls), 3)
            self.assertIn("historial editorial ilegible", str(ctx.exception))
            self.assertIn("OperationalError", str(ctx.exception))

    def test_retry_then_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            _make_db(db)
            real_connect = editorial._connect
            state = {"n": 0}

            def flaky(path):
                state["n"] += 1
                if state["n"] == 1:
                    raise sqlite3.OperationalError("database is locked")
                return real_connect(path)

            with mock.patch.object(editorial, "_connect", side_effect=flaky):
                posts = editorial._previous_posts(db, retries=3, retry_delay=0)
            self.assertEqual(state["n"], 2)
            self.assertEqual(len(posts), 2)

    def test_corrupt_db_is_not_silently_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
            with self.assertRaises(RuntimeError):
                editorial._previous_posts(db, retries=1, retry_delay=0)


if __name__ == "__main__":
    unittest.main()


class SchemaBootstrapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_esquema_se_crea_una_vez_por_ruta(self):
        creados = []

        class FakeConn:
            row_factory = None

            def executescript(self, sql):
                creados.append(sql)

        with mock.patch.object(editorial, "_SCHEMA_READY", set()), \
                mock.patch.object(editorial.sqlite3, "connect", lambda *a, **k: FakeConn()):
            editorial._connect(self.root / "a.db")
            editorial._connect(self.root / "a.db")
            self.assertEqual(len(creados), 1)
            editorial._connect(self.root / "b.db")
            self.assertEqual(len(creados), 2)

    def test_reconectar_db_real_tras_limpiar_cache(self):
        db = self.root / "real.db"
        first = editorial._connect(db)
        first.close()
        editorial._SCHEMA_READY.discard(str(db))
        second = editorial._connect(db)
        try:
            row = second.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
            self.assertIsNotNone(row)
        finally:
            second.close()
