from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import editorial
import editorial_server


class EditorialServerUnit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = editorial_server.make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}

    def test_health(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("deepseek_configured", body)

    def test_audit(self):
        with mock.patch.object(editorial, "list_runs", return_value=[{"id": "r1"}]):
            status, body = self.request("GET", "/api/audit?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(body["runs"], [{"id": "r1"}])

    def test_run_delega_en_generate(self):
        fake = {"ok": True, "run_id": "r1", "draft": {"text": "hola"}}
        with mock.patch.object(editorial, "generate", return_value=fake) as gen:
            status, body = self.request("POST", "/api/run", {"instruction": "qué pasa", "mode": "editorial"})
        self.assertEqual(status, 200)
        self.assertEqual(body["run_id"], "r1")
        self.assertEqual(gen.call_args.kwargs["mode"], "editorial")

    def test_research_fuerza_modo(self):
        with mock.patch.object(editorial, "generate", return_value={"ok": True}) as gen:
            status, _ = self.request("POST", "/api/research", {"instruction": "qué pasa"})
        self.assertEqual(status, 200)
        self.assertEqual(gen.call_args.kwargs["mode"], "research")

    def test_draft_y_verify(self):
        with mock.patch.object(editorial, "draft_for_run", return_value={"ok": True, "run_id": "r1"}) as fn:
            status, body = self.request("POST", "/api/draft", {"run_id": "r1", "angle_id": "local"})
        self.assertEqual(status, 200)
        self.assertEqual(fn.call_args.kwargs["angle_id"], "local")
        with mock.patch.object(editorial, "verify_draft", return_value={"color": "amber"}) as fn:
            status, body = self.request("POST", "/api/verify", {"run_id": "r1", "text": "texto"})
        self.assertEqual(status, 200)
        self.assertEqual(body["stamp"]["color"], "amber")

    def test_publish_deshabilitado_por_defecto(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("CXT_EDITORIAL_ALLOW_PUBLISH", None)
            status, body = self.request("POST", "/api/publish", {"run_id": "r1", "text": "x", "approve": True})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "publish_disabled")

    def test_publish_exige_aprobacion(self):
        status, body = self.request("POST", "/api/publish", {"run_id": "r1", "text": "x"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "approval_required")

    def test_cors_rechaza_origen_externo(self):
        status, body = self.request("GET", "/health", headers={"Origin": "https://evil.test"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "cors")

    def test_options_extension(self):
        status, body = self.request(
            "OPTIONS",
            "/api/run",
            headers={"Origin": "chrome-extension://abc"},
        )
        self.assertEqual(status, 204)

    def test_body_invalido(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/run",
            data=b"{no-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)


class TokenTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patch = mock.patch.object(editorial_server, "_state_dir", return_value=self.home)
        self._patch.start()
        self.token_path = self.home / "editorial.token"

    def tearDown(self):
        if self.token_path.exists():
            os.chmod(self.token_path, 0o600)
        self._patch.stop()
        self._tmp.cleanup()

    def write_token(self, content: str, mode: int = 0o600) -> None:
        self.token_path.write_text(content, encoding="utf-8")
        os.chmod(self.token_path, mode)

    def test_ausente_desactiva_auth(self):
        self.assertEqual(editorial_server._token(), "")
        self.assertEqual(editorial_server._token(strict=True), "")
        self.assertEqual(editorial_server.main(["--check"]), 0)

    def test_token_valido(self):
        self.write_token("abc\n")
        self.assertEqual(editorial_server._token(strict=True), "abc")
        self.assertEqual(editorial_server.main(["--check"]), 0)

    def test_token_vacio_falla_cerrado(self):
        self.write_token("  \n")
        with self.assertRaises(editorial_server.TokenReadError):
            editorial_server._token(strict=True)
        with mock.patch("sys.stderr") as err:
            self.assertEqual(editorial_server.main(["--check"]), 1)
        printed = "".join(str(c) for c in err.write.call_args_list)
        self.assertIn("error:", printed)
        self.assertIn("vacio", printed)

    @unittest.skipIf(os.geteuid() == 0, "root puede leer aunque no tenga permisos")
    def test_token_ilegible_falla_cerrado(self):
        self.write_token("secret-token\n", mode=0o000)
        self.assertEqual(editorial_server._token(), "")
        with self.assertRaises(editorial_server.TokenReadError):
            editorial_server._token(strict=True)
        with mock.patch("sys.stderr") as err:
            self.assertEqual(editorial_server.main(["--check"]), 1)
        printed = "".join(str(c) for c in err.write.call_args_list)
        self.assertIn("error:", printed)
        self.assertNotIn("secret-token", printed)


if __name__ == "__main__":
    unittest.main()
