#!/usr/bin/env python3
"""Tests de profile_manager.py.

Sin Chrome real, sin cxtd real, sin puertos reales: todo va a directorios
temporales y las costuras spawn/kill/alive/probe estan simuladas. El servidor
HTTP se ata a 127.0.0.1:0 (nunca 8798).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import profile_manager as PM  # noqa: E402

CACHE = {
    "Default": {"name": "Jesús", "user_name": "jesus@example.com", "active_time": 300.0},
    "Profile 1": {"name": "Trabajo", "user_name": "work@example.com", "active_time": 900.0},
    "Profile 2": {"name": "Personal", "gaia_name": "personal@example.com", "active_time": 100.0},
    "System Profile": {"name": "System"},
    "Guest Profile": {"name": "Guest"},
    "Profile 99": {"name": "data:image/png;base64,AAAA", "user_name": "x@example.com"},
}


def write_local_state(chrome_dir, cache=None, last_used="Profile 1"):
    chrome = Path(chrome_dir)
    chrome.mkdir(parents=True, exist_ok=True)
    payload = {"profile": {"last_used": last_used, "info_cache": cache if cache is not None else CACHE}}
    (chrome / "Local State").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return chrome / "Local State"


class FakeDaemon:
    """Simula cxtd por puerto y registra arranques/paradas."""

    def __init__(self, *, spawn_online=True):
        self.ports: dict[int, dict] = {}
        self.spawned: list[dict] = []
        self.killed: list[tuple[int, int]] = []
        self.alive: set[int] = set()
        self.pid_port: dict[int, int] = {}
        self.spawn_online = spawn_online
        self.next_pid = 4000

    def probe(self, port, token=""):
        state = self.ports.get(port, {"open": False})
        if not state.get("open"):
            return {"open": False, "health": None, "stat": None}
        return {"open": True, "health": state.get("health"), "stat": state.get("stat")}

    def spawn(self, profile_dir, port, cxt_dir):
        self.next_pid += 1
        pid = self.next_pid
        self.alive.add(pid)
        self.pid_port[pid] = port
        self.spawned.append({"profile_dir": profile_dir, "port": port, "pid": pid, "cxt_dir": str(cxt_dir)})
        self.set_daemon(port, online=self.spawn_online, cxt_dir=str(cxt_dir))
        return {"pid": pid, "process": None}

    def set_daemon(self, port, *, online=True, last_poll_ms=1_700_000_000_000, cxt_dir="", **stat):
        payload = {
            "ok": True,
            "extension_online": online,
            "last_poll_ms": last_poll_ms,
            "queued": 0,
            "pending": 0,
            "inflight": 0,
            "cxt_dir": cxt_dir,
        }
        payload.update(stat)
        self.ports[port] = {"open": True, "health": {"ok": True}, "stat": payload}

    def close(self, port):
        self.ports.pop(port, None)

    def port_open(self, port):
        return bool(self.ports.get(port, {}).get("open"))

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        self.alive.discard(pid)
        port = self.pid_port.pop(pid, None)
        if port is not None:
            self.close(port)

    def alive_fn(self, pid):
        return pid in self.alive


def make_manager(home, chrome, fake, **kwargs):
    return PM.ProfileManager(
        home=home,
        chrome_dir=chrome,
        spawn=fake.spawn,
        killer=fake.kill,
        alive=fake.alive_fn,
        prober=fake.probe,
        port_open=fake.port_open,
        cxtd_bin="/bin/true",
        start_timeout=kwargs.pop("start_timeout", 0.5),
        stop_timeout=kwargs.pop("stop_timeout", 0.3),
        **kwargs,
    )


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cxt_pm_test_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.chrome = self.root / "chrome"


# ---------------------------------------------------------------------------
# 1. Descubrimiento
# ---------------------------------------------------------------------------


class DiscoveryTest(TempHomeCase):
    def test_fixture_perfiles_y_sistema(self):
        write_local_state(self.chrome)
        profiles, error = PM.discover_profiles_ex(self.chrome)
        self.assertIsNone(error)
        by_dir = {row["profile_dir"]: row for row in profiles}
        self.assertEqual(set(by_dir), {"Default", "Profile 1", "Profile 2", "Profile 99"})
        self.assertEqual(by_dir["Default"]["name"], "Jesús")
        self.assertEqual(by_dir["Default"]["email"], "jesus@example.com")
        self.assertEqual(by_dir["Default"]["active_time"], 300.0)
        self.assertTrue(by_dir["Profile 1"]["last_used"])
        self.assertTrue(by_dir["Profile 1"]["is_last_used"])
        self.assertFalse(by_dir["Default"]["is_last_used"])
        self.assertEqual(by_dir["Profile 2"]["email"], "personal@example.com")
        # Un nombre que es un data URI se descarta y cae al profile_dir.
        self.assertEqual(by_dir["Profile 99"]["name"], "Profile 99")
        # Orden estable por nombre.
        self.assertEqual([row["name"] for row in profiles], ["Jesús", "Personal", "Profile 99", "Trabajo"])

    def test_local_state_ausente(self):
        profiles, error = PM.discover_profiles_ex(self.chrome)
        self.assertEqual(profiles, [])
        self.assertIn("no existe", error or "")
        self.assertEqual(PM.discover_profiles(self.chrome), [])

    def test_local_state_json_invalido(self):
        self.chrome.mkdir(parents=True, exist_ok=True)
        (self.chrome / "Local State").write_text("{ esto no es json", encoding="utf-8")
        profiles, error = PM.discover_profiles_ex(self.chrome)
        self.assertEqual(profiles, [])
        self.assertIn("JSON invalido", error or "")

    def test_local_state_bloqueado(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignora los permisos")
        path = write_local_state(self.chrome)
        os.chmod(path, 0)
        try:
            profiles, error = PM.discover_profiles_ex(self.chrome)
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(profiles, [])
        self.assertIn("sin permisos", error or "")

    def test_local_state_solo_lectura(self):
        path = write_local_state(self.chrome)
        before = path.read_bytes()
        before_stat = path.stat()
        PM.discover_profiles_ex(self.chrome)
        PM.discover_profiles(self.chrome)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_omite_nombres_inseguros(self):
        cache = dict(CACHE)
        cache["../evil"] = {"name": "Evil"}
        cache["a/b"] = {"name": "Slash"}
        write_local_state(self.chrome, cache=cache)
        profiles, error = PM.discover_profiles_ex(self.chrome)
        self.assertIsNone(error)
        self.assertNotIn("../evil", {row["profile_dir"] for row in profiles})
        self.assertNotIn("a/b", {row["profile_dir"] for row in profiles})

    def test_profile_key_no_admite_traversal(self):
        self.assertEqual(PM.profile_key("Profile 1"), "Profile_1")
        with self.assertRaises(ValueError):
            PM.profile_key("../../etc")

    def test_autodetecta_chromium_si_no_hay_chrome(self):
        xdg = self.root / "xdg"
        write_local_state(xdg / "chromium")
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(xdg)
        try:
            resolved = PM.resolve_chrome_dir()
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old
        self.assertEqual(resolved, xdg / "chromium")


# ---------------------------------------------------------------------------
# 2. Persistencia
# ---------------------------------------------------------------------------


class PersistenceTest(TempHomeCase):
    def test_registro_de_uso_atomico_concurrente(self):
        path = self.home / "profiles_usage.json"
        errors: list[BaseException] = []
        workers = 6
        iterations = 5

        def worker(_index):
            try:
                for _ in range(iterations):
                    PM.record_usage("Default", "Jesús", "j@example.com", True, path=path, now=1000)
            except BaseException as exc:  # se reporta abajo
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        doc = PM.load_usage(path)
        entry = doc["profiles"]["Default"]
        self.assertEqual(entry["use_count"], workers * iterations)
        self.assertEqual(entry["success_count"], workers * iterations)
        self.assertEqual(entry["failure_count"], 0)
        self.assertEqual(entry["last_success_at"], 1000)
        self.assertEqual(entry["name"], "Jesús")
        self.assertEqual(entry["email"], "j@example.com")
        self.assertEqual(doc["version"], 1)

    def test_fallo_no_cuenta_como_exito(self):
        path = self.home / "profiles_usage.json"
        PM.record_usage("Profile 1", success=True, path=path, now=10)
        PM.record_usage("Profile 1", success=False, path=path, now=20)
        entry = PM.load_usage(path)["profiles"]["Profile 1"]
        self.assertEqual(entry["use_count"], 2)
        self.assertEqual(entry["success_count"], 1)
        self.assertEqual(entry["failure_count"], 1)
        self.assertEqual(entry["last_success_at"], 10)
        self.assertEqual(entry["last_used_at"], 20)

    def test_json_corrupto_se_reemplaza_sin_perder_estructura(self):
        path = self.home / "profiles_usage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("no-json", encoding="utf-8")
        entry = PM.record_usage("Default", path=path, now=1)
        self.assertEqual(entry["use_count"], 1)
        self.assertEqual(PM.load_usage(path)["version"], 1)

    def test_puertos_persistentes(self):
        path = self.home / "profiles_ports.json"
        PM.update_port_entry("Default", path=path, port=8799, cxt_dir="/tmp/a", pid=111, last_started_at=5)
        PM.update_port_entry("Profile 1", path=path, port=8800, cxt_dir="/tmp/b")
        doc = PM.load_ports(path)
        self.assertEqual(doc["profiles"]["Default"]["port"], 8799)
        self.assertEqual(doc["profiles"]["Default"]["pid"], 111)
        self.assertEqual(doc["profiles"]["Profile 1"]["port"], 8800)
        PM.update_port_entry("Default", path=path, clear_pid=True)
        self.assertNotIn("pid", PM.load_ports(path)["profiles"]["Default"])


# ---------------------------------------------------------------------------
# 3. Ranking
# ---------------------------------------------------------------------------


class RankingTest(unittest.TestCase):
    def setUp(self):
        self.profiles = [
            {"profile_dir": "Default", "name": "Jesús", "email": "", "active_time": 1.0, "last_used": False, "is_last_used": False},
            {"profile_dir": "Profile 1", "name": "Trabajo", "email": "", "active_time": 10.0, "last_used": True, "is_last_used": True},
            {"profile_dir": "Profile 2", "name": "Personal", "email": "", "active_time": 5.0, "last_used": False, "is_last_used": False},
            {"profile_dir": "Profile 3", "name": "Viejo", "email": "", "active_time": 50.0, "last_used": False, "is_last_used": False},
            {"profile_dir": "Profile 4", "name": "Nuevo", "email": "", "active_time": 500.0, "last_used": False, "is_last_used": False},
        ]
        self.usage = {
            "version": 1,
            "profiles": {
                "Profile 1": {"use_count": 3, "success_count": 0, "last_used_at": 200},
                "Profile 2": {"use_count": 5, "success_count": 2, "last_used_at": 300},
            },
        }

    def test_orden_actual_usados_otros(self):
        ranked = PM.rank_profiles(self.profiles, self.usage, current_profile_dir="Default")
        self.assertEqual(
            [row["profile_dir"] for row in ranked],
            ["Default", "Profile 2", "Profile 1", "Profile 4", "Profile 3"],
        )
        self.assertEqual([row["group"] for row in ranked], ["actual", "usados", "usados", "otros", "otros"])
        self.assertTrue(ranked[1]["recommended"])
        self.assertFalse(ranked[2]["recommended"])
        self.assertFalse(ranked[0]["recommended"])

    def test_no_infiere_actual_por_last_used(self):
        ranked = PM.rank_profiles(self.profiles, self.usage)
        self.assertNotIn("actual", {row["group"] for row in ranked})
        self.assertEqual(ranked[0]["profile_dir"], "Profile 2")

    def test_actual_no_se_duplica_en_usados(self):
        ranked = PM.rank_profiles(self.profiles, self.usage, current_profile_dir="Profile 2")
        self.assertEqual([row["profile_dir"] for row in ranked].count("Profile 2"), 1)
        self.assertEqual(ranked[0]["group"], "actual")
        self.assertEqual(ranked[0]["use_count"], 5)

    def test_empate_de_uso_por_conteo_y_nombre(self):
        profiles = [
            {"profile_dir": "A", "name": "B", "active_time": 0},
            {"profile_dir": "B", "name": "A", "active_time": 0},
        ]
        usage = {"profiles": {"A": {"use_count": 1, "last_used_at": 5}, "B": {"use_count": 9, "last_used_at": 5}}}
        ranked = PM.rank_profiles(profiles, usage)
        self.assertEqual([row["profile_dir"] for row in ranked], ["B", "A"])

    def test_sin_recomendado_si_no_hay_exitos(self):
        usage = {"profiles": {"Profile 1": {"use_count": 2, "success_count": 0, "last_used_at": 1}}}
        ranked = PM.rank_profiles(self.profiles, usage)
        self.assertFalse(any(row["recommended"] for row in ranked))


# ---------------------------------------------------------------------------
# 4/5. Ciclo de vida + API HTTP
# ---------------------------------------------------------------------------


class ManagerHTTPCase(TempHomeCase):
    def setUp(self):
        super().setUp()
        write_local_state(self.chrome)
        self.fake = FakeDaemon()
        self.manager = make_manager(self.home, self.chrome, self.fake)
        self.server = PM.ManagerHTTPServer(("127.0.0.1", 0), self.manager)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, payload=None, headers=None, raw_body=None):
        if raw_body is not None:
            body = raw_body
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
        else:
            body = None
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=body,
            method=method,
        )
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = dict(exc.headers)
            exc.close()
            return exc.code, (json.loads(raw) if raw else {}), headers

    # -- CORS / auth -------------------------------------------------------

    def test_health_sin_origin(self):
        status, body, _headers = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "profile_manager")
        self.assertFalse(body["token_required"])

    def test_origen_web_rechazado(self):
        status, body, _headers = self.request("GET", "/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "forbidden_origin")
        status2, body2, _headers2 = self.request("GET", "/health", headers={"Origin": "http://127.0.0.1:3000"})
        self.assertEqual(status2, 403)
        self.assertEqual(body2["code"], "forbidden_origin")

    def test_origen_extension_aceptado_y_eco_cors(self):
        status, body, headers = self.request(
            "GET", "/health", headers={"Origin": "chrome-extension://abcdefghijklmnop"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "chrome-extension://abcdefghijklmnop")

    def test_preflight_extension(self):
        status, _body, headers = self.request(
            "OPTIONS", "/select", headers={"Origin": "chrome-extension://abc"}
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "chrome-extension://abc")

    def test_token(self):
        self.manager.token = "s3cr3t"
        try:
            status, body, _headers = self.request("GET", "/health")
            self.assertEqual(status, 401)
            self.assertEqual(body["code"], "unauthorized")
            status2, body2, _headers = self.request("GET", "/health", headers={"X-CXT-Token": "s3cr3t"})
            self.assertEqual(status2, 200)
            self.assertTrue(body2["token_required"])
            status3, _body3, _headers = self.request("GET", "/health", headers={"Authorization": "Bearer s3cr3t"})
            self.assertEqual(status3, 200)
            status3b, _body3b, _headers = self.request("GET", "/health?token=s3cr3t")
            self.assertEqual(status3b, 200)
            status4, _body4, _headers = self.request("GET", "/health", headers={"X-CXT-Token": "malo"})
            self.assertEqual(status4, 401)
        finally:
            self.manager.token = ""

    # -- /profiles ---------------------------------------------------------

    def test_profiles_rank_y_readiness(self):
        status, body, _headers = self.request("GET", "/profiles?current_profile_dir=Default")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        rows = body["profiles"]
        self.assertEqual([row["group"] for row in rows], ["actual", "otros", "otros", "otros"])
        self.assertEqual(rows[0]["profile_dir"], "Default")
        self.assertEqual(rows[0]["status"], "daemon apagado")
        self.assertFalse(rows[0]["ready"])
        self.assertTrue(all(row["port"] is None for row in rows))

    def test_profiles_readiness_cuatro_estados(self):
        PM.update_port_entry("Default", path=self.home / "profiles_ports.json", port=8799)
        self.fake.set_daemon(8799, online=True)
        rows = {row["profile_dir"]: row for row in self.manager.list_profiles()["profiles"]}
        self.assertEqual(rows["Default"]["status"], "listo")
        self.assertTrue(rows["Default"]["ready"])
        self.fake.set_daemon(8799, online=False, last_poll_ms=1_700_000_000_000)
        rows = {row["profile_dir"]: row for row in self.manager.list_profiles()["profiles"]}
        self.assertEqual(rows["Default"]["status"], "ventana cerrada")
        self.fake.set_daemon(8799, online=False, last_poll_ms=0)
        rows = {row["profile_dir"]: row for row in self.manager.list_profiles()["profiles"]}
        self.assertEqual(rows["Default"]["status"], "sin extensión")
        self.fake.close(8799)
        rows = {row["profile_dir"]: row for row in self.manager.list_profiles()["profiles"]}
        self.assertEqual(rows["Default"]["status"], "daemon apagado")
        self.assertFalse(rows["Profile 1"]["ready"])

    def test_profiles_error_si_no_hay_local_state(self):
        empty = self.root / "chrome_vacio"
        manager = make_manager(self.root / "home2", empty, self.fake)
        result = manager.list_profiles()
        self.assertFalse(result["ok"])
        self.assertEqual(result["profiles"], [])
        self.assertIn("no existe", result["error"] or "")

    # -- /select + puertos ------------------------------------------------

    def test_select_asigna_8799_y_persiste(self):
        status, body, _headers = self.request("POST", "/select", payload={"profile_dir": "Default"})
        self.assertEqual(status, 200)
        self.assertEqual(body["port"], 8799)
        self.assertTrue(body["started"])
        self.assertEqual(len(self.fake.spawned), 1)
        self.assertEqual(self.fake.spawned[0]["cxt_dir"], str(self.home / "profiles" / "Default"))
        self.assertTrue((self.home / "profiles" / "Default" / "cxtd.pid").is_file())
        ports = PM.load_ports(self.home / "profiles_ports.json")
        self.assertEqual(ports["profiles"]["Default"]["port"], 8799)
        active = json.loads((self.home / "profiles_active.json").read_text(encoding="utf-8"))
        self.assertEqual(active["active_profile_dir"], "Default")
        self.assertTrue(active["managed"])

    def test_cambio_de_perfil_para_el_trackeado_y_usa_8800(self):
        _status, first, _headers = self.request("POST", "/select", payload={"profile_dir": "Default"})
        pid = first["pid"]
        status, body, _headers = self.request("POST", "/select", payload={"profile_dir": "Profile 1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["port"], 8800)
        self.assertEqual([pid_killed for pid_killed, _sig in self.fake.killed], [pid])
        self.assertEqual([item["port"] for item in self.fake.spawned], [8799, 8800])

    def test_cambio_bloqueado_si_hay_trabajo(self):
        _status, first, _headers = self.request("POST", "/select", payload={"profile_dir": "Default"})
        self.fake.set_daemon(8799, online=True, pending=2)
        status, body, _headers = self.request("POST", "/select", payload={"profile_dir": "Profile 1"})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "busy")
        self.assertEqual(self.fake.killed, [])
        self.assertEqual(len(self.fake.spawned), 1)
        self.assertEqual(body["active_profile_dir"], "Default")
        self.assertTrue(first["ok"])

    def test_reusa_daemon_no_trackeado_sin_matar(self):
        PM.update_port_entry(
            "Default",
            path=self.home / "profiles_ports.json",
            port=8799,
            cxt_dir=str(self.home / "profiles" / "Default"),
        )
        self.fake.set_daemon(8799, online=True, cxt_dir="/otro/cxt_dir")
        status, body, _headers = self.request("POST", "/select", payload={"profile_dir": "Default"})
        self.assertEqual(status, 200)
        self.assertTrue(body["reused"])
        self.assertFalse(body["started"])
        self.assertEqual(self.fake.spawned, [])
        self.assertFalse((self.home / "profiles" / "Default" / "cxtd.pid").exists())
        # Cambiar de perfil no mata lo no gestionado ni arranca un segundo daemon.
        status2, body2, _headers = self.request("POST", "/select", payload={"profile_dir": "Profile 1"})
        self.assertEqual(status2, 409)
        self.assertEqual(body2["code"], "untracked_active")
        self.assertEqual(self.fake.spawned, [])
        self.assertEqual(self.fake.killed, [])

    def test_puerto_con_cxtd_que_exige_token_no_se_duplica(self):
        PM.update_port_entry("Default", path=self.home / "profiles_ports.json", port=8799)

        def probe_auth(port, token=""):
            return {"open": True, "health": None, "stat": None, "auth_required": True}

        self.manager._probe_impl = probe_auth
        with self.assertRaises(PM.ProfileNotReady) as ctx:
            self.manager.ensure_profile_ready("Default")
        self.assertEqual(ctx.exception.code, "auth_mismatch")
        self.assertEqual(self.fake.spawned, [])

    def test_puerto_ocupado_por_otro_servicio_se_reasigna(self):
        PM.update_port_entry("Default", path=self.home / "profiles_ports.json", port=8799)
        self.fake.ports[8799] = {"open": True, "health": None, "stat": None}
        status, body, _headers = self.request("POST", "/select", payload={"profile_dir": "Default"})
        self.assertEqual(status, 200)
        self.assertEqual(body["port"], 8800)
        self.assertEqual(PM.load_ports(self.home / "profiles_ports.json")["profiles"]["Default"]["port"], 8800)

    # -- /enqueue ----------------------------------------------------------

    def test_enqueue_fija_el_perfil_y_su_puerto(self):
        PM.update_port_entry("Profile 2", path=self.home / "profiles_ports.json", port=8800)
        self.fake.set_daemon(8800, online=True)
        captured = {}

        def fake_proxy(profile_dir, port, payload):
            captured.update({"profile_dir": profile_dir, "port": port, "payload": payload})
            return {"ok": True, "status": 200, "data": {"queued": 1}}

        self.manager.proxy_autopost = fake_proxy
        status, body, _headers = self.request(
            "POST",
            "/enqueue",
            payload={"profile_dir": "Profile 2", "action": "add", "url": "https://facebook.com/x"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(captured["profile_dir"], "Profile 2")
        self.assertEqual(captured["port"], 8800)
        self.assertNotIn("profile_dir", captured["payload"])
        self.assertEqual(captured["payload"], {"action": "add", "url": "https://facebook.com/x"})

    def test_enqueue_falla_409_si_no_esta_listo(self):
        self.fake.spawn_online = False
        self.manager.start_timeout = 0.15
        called = []
        self.manager.proxy_autopost = lambda *args: called.append(args) or {"ok": True}
        status, body, _headers = self.request(
            "POST", "/enqueue", payload={"profile_dir": "Default", "action": "add"}
        )
        self.assertEqual(status, 409)
        self.assertIn(body["code"], {"not_ready", "busy"})
        self.assertEqual(called, [])

    def test_enqueue_profile_no_descubierto(self):
        status, body, _headers = self.request(
            "POST", "/enqueue", payload={"profile_dir": "Profile 9", "action": "add"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "not_ready")

    def test_select_traversal_400(self):
        status, body, _headers = self.request("POST", "/select", payload={"profile_dir": "../../etc/passwd"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid")

    def test_body_demasiado_grande(self):
        status, body, _headers = self.request(
            "POST", "/select", raw_body=b"{" + b" " * (PM.MAX_BODY_BYTES + 10) + b"}"
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["code"], "body_too_large")

    # -- /last_run ---------------------------------------------------------

    def test_last_run_lee_el_status_del_perfil(self):
        cxt_dir = self.home / "profiles" / "Default"
        cxt_dir.mkdir(parents=True)
        (cxt_dir / "autopost_status.json").write_text(
            json.dumps({"running": False, "last_run": {"id": "run-1", "ok": True, "shared": 3}}),
            encoding="utf-8",
        )
        status, body, _headers = self.request("GET", "/last_run?profile_dir=Default")
        self.assertEqual(status, 200)
        self.assertEqual(body["last_run"]["id"], "run-1")
        self.assertEqual(body["last_run"]["shared"], 3)

    def test_last_run_fallback_global_si_perfil_activo_no_gestionado(self):
        self.manager._mark_active("Default", 8799, 745, managed=False)
        (self.home / "autopost_status.json").write_text(
            json.dumps({"running": False, "last_run": {"id": "run-global", "ok": True, "shared": 2}}),
            encoding="utf-8",
        )
        status, body, _headers = self.request("GET", "/last_run?profile_dir=Default")
        self.assertEqual(status, 200)
        self.assertEqual(body["last_run"]["id"], "run-global")
        self.assertTrue(body["fallback"])
        self.assertEqual(body["status_file"], str(self.home / "autopost_status.json"))

    def test_last_run_sin_fallback_si_perfil_es_gestionado(self):
        self.manager._mark_active("Default", 8799, 745, managed=True)
        (self.home / "autopost_status.json").write_text(
            json.dumps({"last_run": {"id": "run-global"}}),
            encoding="utf-8",
        )
        status, body, _headers = self.request("GET", "/last_run?profile_dir=Default")
        self.assertEqual(status, 200)
        self.assertIsNone(body["last_run"])
        self.assertNotIn("fallback", body)

    def test_last_run_sin_fallback_si_el_perfil_no_es_el_activo(self):
        self.manager._mark_active("Profile 1", 8800, 999, managed=False)
        (self.home / "autopost_status.json").write_text(
            json.dumps({"last_run": {"id": "run-global"}}),
            encoding="utf-8",
        )
        status, body, _headers = self.request("GET", "/last_run?profile_dir=Default")
        self.assertEqual(status, 200)
        self.assertIsNone(body["last_run"])
        self.assertNotIn("fallback", body)

    def test_last_run_vacio(self):
        status, body, _headers = self.request("GET", "/last_run?profile_dir=Default")
        self.assertEqual(status, 200)
        self.assertIsNone(body["last_run"])
        self.assertNotIn("fallback", body)

    def test_last_run_sin_perfil_400(self):
        status, body, _headers = self.request("GET", "/last_run")
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "missing_profile")

    def test_ruta_desconocida_404(self):
        status, body, _headers = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")


# ---------------------------------------------------------------------------
# Parada / rollback del ciclo de vida
# ---------------------------------------------------------------------------


class LifecycleTest(TempHomeCase):
    def setUp(self):
        super().setUp()
        write_local_state(self.chrome)
        self.fake = FakeDaemon()
        self.manager = make_manager(self.home, self.chrome, self.fake)

    def test_stop_solo_mata_el_pid_trackeado(self):
        self.manager.ensure_profile_ready("Default")
        tracked_pid = self.fake.spawned[0]["pid"]
        # Daemon ajeno en 8800 que nunca fue trackeado por el gestor.
        self.fake.set_daemon(8800, online=True)
        result = self.manager._stop_tracked("Default")
        self.assertTrue(result["stopped"])
        self.assertEqual([pid for pid, _sig in self.fake.killed], [tracked_pid])
        self.assertIn(8800, self.fake.ports)
        self.assertFalse((self.home / "profiles" / "Default" / "cxtd.pid").exists())

    def test_pid_file_huerfano_no_mata_proceso_ajeno(self):
        cxt_dir = self.manager.profile_cxt_dir("Default")
        cxt_dir.mkdir(parents=True)
        ajeno = 999999
        (cxt_dir / "cxtd.pid").write_text(
            json.dumps({"pid": ajeno, "port": 8799, "cxt_dir": str(cxt_dir)}), encoding="utf-8"
        )
        result = self.manager._stop_tracked("Default")
        self.assertFalse(result["stopped"])
        self.assertEqual(self.fake.killed, [])

    def test_no_cambia_si_el_activo_no_tiene_pid_y_el_puerto_sigue_ocupado(self):
        PM.update_port_entry("Default", path=self.home / "profiles_ports.json", port=8799)
        (self.home / "profiles_active.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_profile_dir": "Default",
                    "port": 8799,
                    "pid": 0,
                    "managed": True,
                }
            ),
            encoding="utf-8",
        )
        self.fake.set_daemon(8799, online=True)
        with self.assertRaises(PM.ProfileNotReady) as ctx:
            self.manager.ensure_profile_ready("Profile 1")
        self.assertEqual(ctx.exception.code, "orphan_daemon")
        self.assertEqual(self.fake.spawned, [])
        self.assertEqual(self.fake.killed, [])

    def test_reintento_del_mismo_perfil_reusa_su_daemon(self):
        first = self.manager.ensure_profile_ready("Default")
        second = self.manager.ensure_profile_ready("Default")
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.fake.spawned), 1)

    def test_daemon_muerto_al_arrancar_limpia_estado(self):
        class DeadProcess:
            returncode = 3

            def poll(self):
                return 3

        self.manager._spawn = lambda profile_dir, port, cxt_dir: {"pid": 1234, "process": DeadProcess()}
        with self.assertRaises(PM.ProfileNotReady) as ctx:
            self.manager.ensure_profile_ready("Default")
        self.assertEqual(ctx.exception.code, "daemon_exited")
        self.assertFalse((self.home / "profiles" / "Default" / "cxtd.pid").exists())
        self.assertFalse((self.home / "profiles_active.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
