"""Servidor HTTP local de la mesa editorial CXT."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import editorial

HOST = "127.0.0.1"
PORT = 8795
MAX_BODY = 2 * 1024 * 1024


def _state_dir() -> Path:
    return Path.home() / ".cxt"


def _token() -> str:
    path = _state_dir() / "editorial.token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_cmd(payload: dict) -> None:
    target = _state_dir() / "schedule.cmd.json"
    if target.exists():
        raise FileExistsError("hay un comando de programación pendiente")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".schedule-cmd-", suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "CXTEditorial/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[editorial] {self.address_string()} {fmt % args}", flush=True)

    def _origin(self) -> str:
        return self.headers.get("Origin", "").strip()

    def _cors_headers(self) -> dict:
        origin = self._origin()
        headers = {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-CXT-Token",
            "Cache-Control": "no-store",
        }
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers

    def _allowed(self) -> bool:
        origin = self._origin()
        if not origin or origin.startswith("chrome-extension://"):
            return True
        if os.environ.get("CXT_EDITORIAL_DEV") == "1":
            return origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
        return False

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._send(status, {"ok": False, "error": message, "code": code})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("cuerpo demasiado grande")
        raw = self.rfile.read(length)
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("el cuerpo debe ser un objeto JSON")
        return doc

    def _check_token(self) -> bool:
        expected = _token()
        if not expected:
            return True
        return self.headers.get("X-CXT-Token", "") == expected

    def do_OPTIONS(self) -> None:
        if not self._allowed():
            self._error(403, "origen no permitido", "cors")
            return
        self.send_response(204)
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._allowed():
            self._error(403, "origen no permitido", "cors")
            return
        if not self._check_token():
            self._error(401, "token inválido", "auth")
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {
                "ok": True,
                "version": "1.0.0",
                "deepseek_configured": bool(editorial.deepseek_key()),
                "db": str(editorial.DEFAULT_DB),
            })
            return
        if parsed.path == "/api/audit":
            try:
                limit = int((parse_qs(parsed.query).get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            self._send(200, {"ok": True, "runs": editorial.list_runs(limit=limit)})
            return
        if parsed.path == "/api/run":
            run_id = (parse_qs(parsed.query).get("id") or [""])[0].strip()
            if not run_id:
                self._error(400, "falta id", "missing_id")
                return
            try:
                run = editorial.get_run(run_id)
            except Exception as exc:
                self._error(500, f"no se pudo leer: {type(exc).__name__}", "read")
                return
            if not run:
                self._error(404, "run no encontrado", "not_found")
                return
            self._send(200, run)
            return
        self._error(404, "ruta no encontrada", "not_found")

    def do_POST(self) -> None:
        if not self._allowed():
            self._error(403, "origen no permitido", "cors")
            return
        if not self._check_token():
            self._error(401, "token inválido", "auth")
            return
        parsed = urlparse(self.path)
        try:
            body = self._body()
            if parsed.path in ("/api/run", "/api/research"):
                instruction = str(body.get("instruction") or "").strip()
                if not instruction:
                    self._error(400, "falta instruction", "missing_instruction")
                    return
                mode = "research" if parsed.path == "/api/research" else str(body.get("mode") or "editorial")
                result = editorial.generate(
                    instruction,
                    mode=mode,
                    freshness_hours=int(body.get("freshness_hours") or 48),
                    min_sources=int(body.get("min_sources") or 2),
                    tone=str(body.get("tone") or ""),
                    audience=str(body.get("audience") or ""),
                    must_include=[str(x) for x in (body.get("must_include") or []) if str(x).strip()],
                    must_not=[str(x) for x in (body.get("must_not") or []) if str(x).strip()],
                    max_length=int(body.get("max_length") or 900),
                )
                self._send(200, result)
                return
            if parsed.path == "/api/draft":
                run_id = str(body.get("run_id") or "").strip()
                if not run_id:
                    self._error(400, "falta run_id", "missing_run")
                    return
                result = editorial.draft_for_run(run_id, angle_id=body.get("angle_id"))
                self._send(200, result)
                return
            if parsed.path == "/api/verify":
                run_id = str(body.get("run_id") or "").strip()
                text = str(body.get("text") or "").strip()
                if not run_id or not text:
                    self._error(400, "faltan run_id o text", "missing_data")
                    return
                stamp = editorial.verify_draft(run_id, text, angle_id=body.get("angle_id"))
                self._send(200, {"ok": True, "stamp": stamp})
                return
            if parsed.path == "/api/publish":
                run_id = str(body.get("run_id") or "").strip()
                text = str(body.get("text") or "").strip()
                if body.get("approve") is not True:
                    self._error(403, "falta aprobación explícita", "approval_required")
                    return
                if os.environ.get("CXT_EDITORIAL_ALLOW_PUBLISH") != "1":
                    self._error(403, "publicación deshabilitada; activa CXT_EDITORIAL_ALLOW_PUBLISH=1", "publish_disabled")
                    return
                run = editorial.get_run(run_id)
                if not run:
                    self._error(404, "run no encontrado", "not_found")
                    return
                stamp = run.get("stamp") or {}
                draft = run.get("draft") or {}
                if stamp.get("color") != "green" or not stamp.get("publishable"):
                    self._error(409, "el sello no autoriza la publicación", "stamp_blocked")
                    return
                if not text or text != (draft.get("text") or "").strip():
                    self._error(409, "el texto no coincide con el borrador aprobado", "text_mismatch")
                    return
                try:
                    _write_cmd({
                        "action": "publish_now",
                        "topic": "fixed",
                        "text": text,
                        "meta": {
                            "run_id": run_id,
                            "stamp_color": stamp.get("color", ""),
                            "publishable": bool(stamp.get("publishable")),
                            "attempt": stamp.get("attempt", 0),
                        },
                    })
                except FileExistsError:
                    self._error(409, "hay un comando de programación pendiente", "cmd_pending")
                    return
                conn = editorial._connect(editorial.DEFAULT_DB)
                try:
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO publications VALUES (?,?,?,?)",
                            (run_id, editorial.new_run_id(), "queued", "aprobado desde la mesa editorial"),
                        )
                finally:
                    conn.close()
                self._send(200, {"ok": True, "published": False, "queued": True, "message": "Aprobado y encolado para publicar."})
                return
            self._error(404, "ruta no encontrada", "not_found")
        except ValueError as exc:
            self._error(400, str(exc), "invalid")
        except KeyError as exc:
            self._error(404, str(exc), "not_found")
        except Exception as exc:
            self._error(500, f"fallo interno: {type(exc).__name__}", "internal")


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Servidor local de la mesa editorial CXT")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--check", action="store_true", help="comprueba configuración y sale")
    args = parser.parse_args(argv)
    if args.check:
        print(json.dumps({
            "ok": True,
            "host": args.host,
            "port": args.port,
            "deepseek_configured": bool(editorial.deepseek_key()),
            "db": str(editorial.DEFAULT_DB),
            "publish_enabled": os.environ.get("CXT_EDITORIAL_ALLOW_PUBLISH") == "1",
        }, ensure_ascii=False))
        return 0
    server = make_server(args.host, args.port)
    print(f"editorial server en http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
