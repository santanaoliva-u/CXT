#!/usr/bin/env python3
# allow: SIZE_OK — la tarea exige un unico modulo con descubrimiento,
# persistencia, ranking, ciclo de vida P1 y API HTTP. Se mantiene por funciones.
"""Gestor de perfiles de Chrome para CXT (P1).

Responsabilidades:
  1. Descubrir perfiles leyendo ``Local State`` de Chrome en solo lectura.
  2. Persistir uso y puertos por perfil de forma atomica y con ``flock``.
  3. Rankear perfiles (Actual / Usados por CXT / Otros).
  4. Mantener **un** ``cxtd`` activo a la vez, por perfil, con PID propio.
  5. Exponer una API HTTP local para la extension (127.0.0.1:8798).

Solo stdlib. No escribe jamas en ``Local State`` ni copia cookies/sesiones.
Las rutas de estado viven bajo ``CXT_PM_HOME`` (por defecto ``~/.cxt``); los
tests lo apuntan a un directorio temporal para no tocar datos reales.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hmac
import http.client
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"

DEFAULT_MANAGER_PORT = 8798
DEFAULT_DAEMON_PORT = 8799
MAX_BODY_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

# Carpetas de perfil que Chrome crea para uso interno y no son cuentas.
SYSTEM_PROFILES = {"system profile", "guest profile"}
PROFILE_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

CHROME_DIR_NAMES = (
    "google-chrome",
    "chromium",
    "google-chrome-beta",
    "google-chrome-unstable",
)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

log = logging.getLogger("cxt.profile_manager")


# ---------------------------------------------------------------------------
# Errores tipados (nunca `except: pass`)
# ---------------------------------------------------------------------------


class ProfileManagerError(Exception):
    """Base de errores controlados del gestor."""


class ProfileNotReady(ProfileManagerError):
    """El perfil no puede usarse: daemon apagado, sin extension o puerto ocupado."""

    def __init__(self, reason: str, code: str = "not_ready", **extra: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.extra = extra


class ProfileBusy(ProfileNotReady):
    """Hay un trabajo en curso en el perfil activo; cambiar ahora lo cortaria."""

    def __init__(self, reason: str, **extra: object) -> None:
        super().__init__(reason, code="busy", **extra)


class UpstreamError(ProfileManagerError):
    """El ``cxtd`` del perfil no acepto el trabajo."""

    def __init__(self, reason: str, status: int = 502, **extra: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.extra = extra


class TokenReadError(ProfileManagerError):
    """El token existe pero no se puede leer: el arranque falla cerrado."""


class _HTTPError(Exception):
    """Error que se traduce directo a una respuesta HTTP."""

    def __init__(self, status: int, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Rutas y utilidades de JSON atomico
# ---------------------------------------------------------------------------


def cxt_home(override: str | os.PathLike[str] | None = None) -> Path:
    """Carpeta de estado del gestor (``CXT_PM_HOME`` o ``~/.cxt``)."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get("CXT_PM_HOME") or os.environ.get("CXT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cxt"


def profile_key(profile_dir: str) -> str:
    """Clave segura y estable para ``profiles/<clave>/`` (sin traversal)."""
    if not _safe_profile_dir(profile_dir):
        raise ValueError("profile_dir invalido: %r" % (profile_dir,))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", profile_dir).strip("._-") or "Default"


def _safe_profile_dir(profile_dir: object) -> bool:
    if not isinstance(profile_dir, str):
        return False
    value = profile_dir.strip()
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        return False
    return bool(PROFILE_DIR_RE.match(value))


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _clean_text(value: object, limit: int = 160) -> str:
    """Normaliza texto de Chrome y descarta valores gigantes (avatares base64)."""
    text = str(value or "").strip()
    if text.startswith("data:"):
        return ""
    if len(text) > limit:
        return text[:limit].rstrip()
    return text


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _load_json(path: Path, default_factory) -> dict:
    """Lee un JSON de objeto; ausente/corrupto -> default (nunca lanza)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
    except FileNotFoundError:
        return default_factory()
    except json.JSONDecodeError as exc:
        log.warning("json invalido en %s: %s", path, exc)
        return default_factory()
    except OSError as exc:
        log.warning("no se pudo leer %s: %s", path, exc)
        return default_factory()
    if not isinstance(value, dict):
        log.warning("json no es objeto en %s", path)
        return default_factory()
    return value


def _atomic_write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    """Escritura atomica: temp en el mismo directorio + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _file_lock(path: Path):
    """Lock exclusivo entre procesos (y entre hilos con fds distintos)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _default_usage() -> dict:
    return {"version": 1, "profiles": {}}


def _default_ports() -> dict:
    return {"version": 1, "profiles": {}}


def _default_active() -> dict:
    return {"version": 1, "active_profile_dir": "", "port": 0, "pid": 0, "managed": False}


def _usage_path(path: Path | None = None) -> Path:
    return path if path is not None else cxt_home() / "profiles_usage.json"


def _ports_path(path: Path | None = None) -> Path:
    return path if path is not None else cxt_home() / "profiles_ports.json"


def _normalize_usage_doc(doc: dict) -> dict:
    out = _default_usage()
    profiles = doc.get("profiles")
    if isinstance(profiles, dict):
        out["profiles"] = {str(k): v for k, v in profiles.items() if isinstance(v, dict)}
    return out


def _normalize_ports_doc(doc: dict) -> dict:
    out = _default_ports()
    profiles = doc.get("profiles")
    if isinstance(profiles, dict):
        out["profiles"] = {str(k): v for k, v in profiles.items() if isinstance(v, dict)}
    return out


# ---------------------------------------------------------------------------
# 1. Descubrimiento (solo lectura)
# ---------------------------------------------------------------------------


def resolve_chrome_dir(chrome_dir: str | os.PathLike[str] | None = None) -> Path:
    """Carpeta de Chrome a usar: explicita, ``CXT_CHROME_DIR`` o autodeteccion."""
    if chrome_dir:
        return Path(chrome_dir).expanduser()
    env = os.environ.get("CXT_CHROME_DIR")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    for name in CHROME_DIR_NAMES:
        candidate = base / name
        if (candidate / "Local State").is_file():
            return candidate
    return base / CHROME_DIR_NAMES[0]


def discover_profiles_ex(
    chrome_dir: str | os.PathLike[str] | None = None,
) -> tuple[list[dict], str | None]:
    """Descubre perfiles y devuelve ``(perfiles, error_legible|None)``.

    Tolerante a Local State ausente, bloqueado, cambiando o con JSON invalido.
    Jamas escribe el archivo. ``Default`` se acepta; los perfiles de sistema no.
    """
    path = resolve_chrome_dir(chrome_dir) / "Local State"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return [], "no existe %s" % path
    except PermissionError:
        return [], "sin permisos para leer %s" % path
    except IsADirectoryError:
        return [], "%s no es un archivo" % path
    except json.JSONDecodeError as exc:
        return [], "JSON invalido en %s: %s" % (path, exc)
    except OSError as exc:
        return [], "no se pudo leer %s: %s" % (path, exc)

    if not isinstance(raw, dict):
        return [], "%s no contiene un objeto JSON" % path
    profile_section = raw.get("profile")
    if not isinstance(profile_section, dict):
        return [], "%s no tiene 'profile'" % path
    cache = profile_section.get("info_cache")
    if not isinstance(cache, dict):
        return [], "%s no tiene 'profile.info_cache'" % path
    last_used = profile_section.get("last_used")
    if not isinstance(last_used, str):
        last_used = ""

    profiles: list[dict] = []
    for key, info in cache.items():
        if not _safe_profile_dir(key):
            log.warning("perfil omitido por nombre inseguro: %r", key)
            continue
        if key.strip().lower() in SYSTEM_PROFILES:
            continue
        if not isinstance(info, dict):
            info = {}
        name = _clean_text(info.get("name")) or key
        email = _clean_text(info.get("user_name") or info.get("gaia_name"), 200)
        profiles.append(
            {
                "profile_dir": key,
                "name": name,
                "email": email,
                "active_time": _coerce_float(info.get("active_time")),
                "last_used": key == last_used,
                "is_last_used": key == last_used,
            }
        )
    profiles.sort(key=lambda row: (row["name"].lower(), row["profile_dir"]))
    return profiles, None


def discover_profiles(chrome_dir: str | os.PathLike[str] | None = None) -> list[dict]:
    """Version simple de :func:`discover_profiles_ex` (lista vacia ante error)."""
    profiles, error = discover_profiles_ex(chrome_dir)
    if error:
        log.warning("descubrimiento de perfiles: %s", error)
    return profiles


# ---------------------------------------------------------------------------
# 2. Persistencia: usage y ports
# ---------------------------------------------------------------------------


def _locked_rw(path: Path, default_factory, normalize, mutate) -> dict:
    """Read-modify-write bajo ``flock`` con escritura atomica."""
    lock_path = path.with_name(path.name + ".lock")
    with _file_lock(lock_path):
        doc = normalize(_load_json(path, default_factory))
        result = mutate(doc)
        _atomic_write_json(path, doc)
        return result if isinstance(result, dict) else doc


def load_usage(path: Path | None = None) -> dict:
    """Carga ``profiles_usage.json`` normalizado (version 1)."""
    return _normalize_usage_doc(_load_json(_usage_path(path), _default_usage))


def load_ports(path: Path | None = None) -> dict:
    """Carga ``profiles_ports.json`` normalizado (version 1)."""
    return _normalize_ports_doc(_load_json(_ports_path(path), _default_ports))


def _get_port_entry(profile_dir: str, path: Path | None = None) -> dict:
    entry = load_ports(path)["profiles"].get(profile_dir)
    return dict(entry) if isinstance(entry, dict) else {}


def record_usage(
    profile_dir: str,
    name: str = "",
    email: str = "",
    success: bool | None = None,
    *,
    path: Path | None = None,
    now: float | None = None,
) -> dict:
    """Actualiza el uso de un perfil sin perder actualizaciones concurrentes.

    ``use_count`` sube siempre (al iniciar un trabajo); ``success`` decide el
    contador de exito/fallo y ``last_success_at``.
    """
    if not _safe_profile_dir(profile_dir):
        raise ValueError("profile_dir invalido: %r" % (profile_dir,))
    target = _usage_path(path)
    stamp = int(now if now is not None else time.time())

    def mutate(doc: dict) -> dict:
        entry = dict(doc["profiles"].get(profile_dir) or {})
        if name:
            entry["name"] = name
        if email:
            entry["email"] = email
        entry.setdefault("name", name or profile_dir)
        entry.setdefault("email", email)
        entry["last_used_at"] = stamp
        entry["use_count"] = _coerce_int(entry.get("use_count")) + 1
        entry["success_count"] = _coerce_int(entry.get("success_count"))
        entry["failure_count"] = _coerce_int(entry.get("failure_count"))
        if success is True:
            entry["success_count"] += 1
            entry["last_success_at"] = stamp
        elif success is False:
            entry["failure_count"] += 1
        doc["profiles"][profile_dir] = entry
        return entry

    return _locked_rw(target, _default_usage, _normalize_usage_doc, mutate)


def update_port_entry(
    profile_dir: str,
    *,
    path: Path | None = None,
    port: int | None = None,
    cxt_dir: str | None = None,
    pid: int | None = None,
    clear_pid: bool = False,
    last_started_at: int | None = None,
) -> dict:
    """Crea/actualiza la entrada de puertos de un perfil (atomico + lock)."""
    if not _safe_profile_dir(profile_dir):
        raise ValueError("profile_dir invalido: %r" % (profile_dir,))
    target = _ports_path(path)

    def mutate(doc: dict) -> dict:
        entry = dict(doc["profiles"].get(profile_dir) or {})
        if port is not None:
            entry["port"] = int(port)
        if cxt_dir is not None:
            entry["cxt_dir"] = str(cxt_dir)
        if last_started_at is not None:
            entry["last_started_at"] = int(last_started_at)
        if clear_pid:
            entry.pop("pid", None)
        elif pid is not None:
            entry["pid"] = int(pid)
        doc["profiles"][profile_dir] = entry
        return entry

    return _locked_rw(target, _default_ports, _normalize_ports_doc, mutate)


# ---------------------------------------------------------------------------
# 3. Ranking
# ---------------------------------------------------------------------------


def _usage_map(usage: object) -> dict:
    if isinstance(usage, dict) and isinstance(usage.get("profiles"), dict):
        return {str(k): v for k, v in usage["profiles"].items() if isinstance(v, dict)}
    if isinstance(usage, dict):
        return {str(k): v for k, v in usage.items() if isinstance(v, dict)}
    return {}


def rank_profiles(
    profiles: list[dict],
    usage: object,
    current_profile_dir: str | None = None,
) -> list[dict]:
    """Ordena Actual -> Usados por CXT -> Otros y anota ``group``/``recommended``.

    * Actual: solo si ``current_profile_dir`` coincide (nunca se infiere por
      ``last_used`` de Chrome).
    * Usados: ``use_count > 0``, por ``last_used_at`` desc y luego ``use_count``.
    * Otros: por ``active_time`` desc y luego nombre.
    """
    usage_by_dir = _usage_map(usage)
    explicit_current = current_profile_dir if _safe_profile_dir(current_profile_dir) else ""
    current: list[dict] = []
    used: list[dict] = []
    others: list[dict] = []

    for raw in profiles:
        if not isinstance(raw, dict):
            continue
        profile_dir = str(raw.get("profile_dir") or "")
        if not _safe_profile_dir(profile_dir):
            continue
        stats = usage_by_dir.get(profile_dir) or {}
        row = dict(raw)
        row["profile_dir"] = profile_dir
        row["name"] = str(raw.get("name") or profile_dir)
        row["email"] = str(raw.get("email") or "")
        row["use_count"] = _coerce_int(stats.get("use_count"))
        row["success_count"] = _coerce_int(stats.get("success_count"))
        row["failure_count"] = _coerce_int(stats.get("failure_count"))
        row["last_used_at"] = _coerce_int(stats.get("last_used_at"))
        row["last_success_at"] = _coerce_int(stats.get("last_success_at"))
        row["recommended"] = False
        if explicit_current and profile_dir == explicit_current:
            row["group"] = "actual"
            current.append(row)
        elif row["use_count"] > 0:
            row["group"] = "usados"
            used.append(row)
        else:
            row["group"] = "otros"
            others.append(row)

    used.sort(key=lambda r: (-r["last_used_at"], -r["use_count"], r["name"].lower(), r["profile_dir"]))
    others.sort(key=lambda r: (-_coerce_float(r.get("active_time")), r["name"].lower(), r["profile_dir"]))
    for row in used:
        if row["success_count"] > 0:
            row["recommended"] = True
            break

    ranked = current + used + others
    for index, row in enumerate(ranked):
        row["rank"] = index
    return ranked


# ---------------------------------------------------------------------------
# HTTP helper (para hablar con cxtd y para la API)
# ---------------------------------------------------------------------------


def http_json(
    port: int,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    *,
    token: str = "",
    host: str = "127.0.0.1",
    timeout: float = 2.0,
) -> dict:
    """Llama a un endpoint JSON local; devuelve ``{ok,status,data|error}``."""
    headers = {"Accept": "application/json"}
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-CXT-Token"] = token
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES)
        data: object = {}
        if raw:
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                data = {"_raw": raw.decode("utf-8", "replace")[:500]}
        return {"ok": 200 <= response.status < 300, "status": response.status, "data": data}
    except (OSError, http.client.HTTPException) as exc:
        return {"ok": False, "status": 0, "error": str(exc), "data": {}}
    finally:
        conn.close()


def _read_token(path: Path, *, strict: bool = False) -> str:
    """Lee un token (0600 esperado). Ausente -> cadena vacia.

    Con ``strict=True`` un token presente pero ilegible o vacio lanza
    ``TokenReadError``: es un error de configuracion, no una forma de
    desactivar la autenticacion. La ausencia del archivo sigue siendo el
    mecanismo explicito para desactivarla.
    """
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        if strict:
            raise TokenReadError("no se pudo leer token %s: %s" % (path, exc)) from exc
        log.warning("no se pudo leer token %s: %s", path, exc)
        return ""
    if stat.st_mode & 0o077:
        log.warning("token %s con permisos amplios (esperado 0600)", path)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        if strict:
            raise TokenReadError("no se pudo leer token %s: %s" % (path, exc)) from exc
        log.warning("no se pudo leer token %s: %s", path, exc)
        return ""
    if strict and not value:
        raise TokenReadError(
            "token %s vacio; elimina el archivo para desactivar la autenticacion" % path
        )
    return value


def resolve_cxtd() -> str:
    """Ruta al binario ``cxtd`` (env, repo/bin o PATH)."""
    for env_name in ("CXT_DAEMON_BIN", "CXT_CXTD"):
        value = os.environ.get(env_name)
        if value:
            return value
    here = Path(__file__).resolve().parent
    for candidate in (here / "bin" / "cxtd", here.parent / "bin" / "cxtd", cxt_home() / "bin" / "cxtd"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("cxtd") or ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_cmdline(pid: int) -> str | None:
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 4. Ciclo de vida P1 + 5. API (clase gestora)
# ---------------------------------------------------------------------------


class ProfileManager:
    """Estado y operaciones del gestor.

    Las costuras ``spawn``/``killer``/``alive``/``prober``/``mono``/``sleep``
    existen para tests sin Chrome ni cxtd reales.
    """

    def __init__(
        self,
        *,
        home: str | os.PathLike[str] | None = None,
        chrome_dir: str | os.PathLike[str] | None = None,
        spawn=None,
        killer=None,
        alive=None,
        prober=None,
        port_open=None,
        mono=None,
        sleep=None,
        start_timeout: float | None = None,
        stop_timeout: float | None = None,
        cxtd_bin: str | None = None,
    ) -> None:
        self.home = cxt_home(home)
        self.chrome_dir = chrome_dir
        self._spawn = spawn or self._default_spawn
        self._kill_impl = killer or os.kill
        self._alive_impl = alive
        self._probe_impl = prober or self._default_probe
        self._port_open_impl = port_open or self._port_open
        self._mono = mono or time.monotonic
        self._sleep = sleep or time.sleep
        self.start_timeout = float(start_timeout if start_timeout is not None else os.environ.get("CXT_P1_START_TIMEOUT", "20"))
        self.stop_timeout = float(stop_timeout if stop_timeout is not None else os.environ.get("CXT_P1_STOP_TIMEOUT", "10"))
        self.cxtd_bin = cxtd_bin if cxtd_bin is not None else resolve_cxtd()
        self.token = _read_token(self.home / "profile_manager.token", strict=True)
        self.started_at = time.time()

    # -- rutas -------------------------------------------------------------

    @property
    def usage_path(self) -> Path:
        return self.home / "profiles_usage.json"

    @property
    def ports_path(self) -> Path:
        return self.home / "profiles_ports.json"

    @property
    def active_path(self) -> Path:
        return self.home / "profiles_active.json"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def profiles_root(self) -> Path:
        return self.home / "profiles"

    def profile_cxt_dir(self, profile_dir: str) -> Path:
        return self.profiles_root / profile_key(profile_dir)

    def _pid_file(self, profile_dir: str) -> Path:
        return self.profile_cxt_dir(profile_dir) / "cxtd.pid"

    def _manager_lock_path(self) -> Path:
        return self.home / "profile_manager.lock"

    # -- discovery/ranking -------------------------------------------------

    def _discover(self) -> tuple[list[dict], str | None]:
        return discover_profiles_ex(self.chrome_dir)

    def _require_profile(self, profile_dir: object) -> dict:
        if not _safe_profile_dir(profile_dir):
            raise ValueError("profile_dir invalido")
        profiles, error = self._discover()
        for row in profiles:
            if row["profile_dir"] == profile_dir:
                return row
        detail = (": " + error) if error else ""
        raise ProfileNotReady("perfil no disponible: %s%s" % (profile_dir, detail), profile_dir=profile_dir)

    def _readiness(self, profile_dir: str) -> dict:
        entry = _get_port_entry(profile_dir, self.ports_path)
        port = _coerce_int(entry.get("port"))
        if not port:
            return {"ready": False, "status": "daemon apagado", "port": None, "tracked": False, "reason": "sin puerto asignado"}
        status = self._status_for(profile_dir, port)
        return {
            "ready": status["ready"],
            "status": status["status"],
            "port": port,
            "tracked": status["tracked"],
            "daemon_pid": status["pid"] or None,
            "reason": status.get("reason"),
        }

    def list_profiles(self, current_profile_dir: str | None = None) -> dict:
        """Lista rankeada con estado de disponibilidad (no arranca nada)."""
        profiles, error = self._discover()
        usage = load_usage(self.usage_path)
        ranked = rank_profiles(profiles, usage, current_profile_dir)
        for row in ranked:
            row.update(self._readiness(row["profile_dir"]))
        return {
            "ok": error is None or bool(ranked),
            "profiles": ranked,
            "current_profile_dir": current_profile_dir or "",
            "error": error,
        }

    # -- estado de un perfil ----------------------------------------------

    def _status_for(self, profile_dir: str, port: int) -> dict:
        cxt_dir = self.profile_cxt_dir(profile_dir)
        pid_info = self._read_pid_file(profile_dir)
        pid = _coerce_int(pid_info.get("pid"))
        tracked = bool(pid and self._alive(pid))
        token = _read_token(cxt_dir / "token") or _read_token(self.home / "token")
        probe = self._probe_impl(port, token)
        port_open = bool(probe.get("open"))
        health = probe.get("health") or {}
        stat = probe.get("stat") or {}
        daemon = bool(port_open and isinstance(health, dict) and health.get("ok") and isinstance(stat, dict) and stat)
        online = bool(stat.get("extension_online")) if daemon else False
        last_poll = _coerce_int(stat.get("last_poll_ms")) if daemon else 0
        reason = None
        auth_required = bool(probe.get("auth_required"))
        if daemon:
            if online:
                status = "listo"
            elif last_poll > 0:
                status = "ventana cerrada"
            else:
                status = "sin extensión"
        else:
            status = "daemon apagado"
            if port_open:
                reason = (
                    "cxtd en el puerto exige un token que este perfil no puede verificar"
                    if auth_required
                    else "puerto ocupado por un servicio que no es cxtd"
                )
        return {
            "ready": status == "listo",
            "status": status,
            "port": port,
            "port_open": port_open,
            "daemon_alive": daemon,
            "tracked": tracked,
            "pid": pid if tracked else 0,
            "extension_online": online,
            "stat": stat,
            "reason": reason,
            "auth_required": auth_required,
        }

    def _payload(self, profile_dir: str, port: int, status: dict, *, started: bool) -> dict:
        return {
            "ok": True,
            "profile_dir": profile_dir,
            "profile_name": self._profile_name(profile_dir),
            "port": port,
            "cxt_dir": str(self.profile_cxt_dir(profile_dir)),
            "pid": status.get("pid") or None,
            "ready": status["ready"],
            "status": status["status"],
            "extension_online": status["extension_online"],
            "tracked": status["tracked"],
            "started": started,
            "reused": not started,
        }

    def _profile_name(self, profile_dir: str) -> str:
        for row in self._discover()[0]:
            if row["profile_dir"] == profile_dir:
                return row["name"]
        return profile_dir

    # -- PID files ---------------------------------------------------------

    def _read_pid_file(self, profile_dir: str) -> dict:
        return _load_json(self._pid_file(profile_dir), dict)

    def _write_pid_file(self, profile_dir: str, *, pid: int, port: int, cxt_dir: Path, systemd_unit: str = "") -> None:
        data = {
            "version": 1,
            "pid": int(pid),
            "port": int(port),
            "cxt_dir": str(cxt_dir),
            "profile_dir": profile_dir,
            "started_at": int(time.time()),
        }
        if systemd_unit:
            data["systemd_unit"] = systemd_unit
        _atomic_write_json(self._pid_file(profile_dir), data)

    def _clear_pid_file(self, profile_dir: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._pid_file(profile_dir).unlink()

    # -- active ------------------------------------------------------------

    def _load_active(self) -> dict:
        active = _load_json(self.active_path, _default_active)
        active.setdefault("active_profile_dir", "")
        active.setdefault("managed", False)
        return active

    def _mark_active(self, profile_dir: str, port: int, pid: int | None, *, managed: bool) -> None:
        _atomic_write_json(
            self.active_path,
            {
                "version": 1,
                "active_profile_dir": profile_dir,
                "port": int(port),
                "pid": int(pid or 0),
                "managed": bool(managed),
                "updated_at": int(time.time()),
            },
        )

    def _clear_active(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.active_path.unlink()

    # -- puertos -----------------------------------------------------------

    def _ensure_port_entry(self, profile_dir: str) -> dict:
        existing = _get_port_entry(profile_dir, self.ports_path)
        port = _coerce_int(existing.get("port"))
        if 0 < port <= 65535 and port != DEFAULT_MANAGER_PORT:
            return existing
        ports = load_ports(self.ports_path)["profiles"]
        assigned = {
            _coerce_int(entry.get("port"))
            for other, entry in ports.items()
            if other != profile_dir and _coerce_int(entry.get("port")) > 0
        }
        candidate = DEFAULT_DAEMON_PORT
        while candidate in assigned or candidate == DEFAULT_MANAGER_PORT:
            candidate += 1
        return update_port_entry(
            profile_dir,
            path=self.ports_path,
            port=candidate,
            cxt_dir=str(self.profile_cxt_dir(profile_dir)),
        )

    def _reassign_port(self, profile_dir: str, avoid: int) -> dict:
        ports = load_ports(self.ports_path)["profiles"]
        assigned = {
            _coerce_int(entry.get("port"))
            for other, entry in ports.items()
            if other != profile_dir and _coerce_int(entry.get("port")) > 0
        }
        candidate = DEFAULT_DAEMON_PORT
        while candidate in assigned or candidate in {avoid, DEFAULT_MANAGER_PORT}:
            candidate += 1
        log.warning("perfil %s: puerto %s ocupado, reasignando a %s", profile_dir, avoid, candidate)
        return update_port_entry(
            profile_dir,
            path=self.ports_path,
            port=candidate,
            cxt_dir=str(self.profile_cxt_dir(profile_dir)),
        )

    # -- trabajo en curso --------------------------------------------------

    def _job_in_progress(self, profile_dir: str) -> bool:
        entry = _get_port_entry(profile_dir, self.ports_path)
        port = _coerce_int(entry.get("port"))
        if not port:
            return False
        status = self._status_for(profile_dir, port)
        if not status["daemon_alive"]:
            return False
        stat = status.get("stat") or {}
        if any(_coerce_int(stat.get(key)) > 0 for key in ("queued", "pending", "inflight")):
            return True
        doc = _load_json(self.profile_cxt_dir(profile_dir) / "autopost_status.json", dict)
        return bool(doc.get("running")) or _coerce_int(doc.get("pending")) > 0

    # -- parada ------------------------------------------------------------

    def _alive(self, pid: int) -> bool:
        if self._alive_impl is not None:
            return bool(self._alive_impl(pid))
        return _pid_alive(pid)

    def _pid_is_ours(self, pid: int) -> bool:
        if self._alive_impl is not None:
            return True  # en tests la liveness es simulada
        cmdline = _read_cmdline(pid)
        if cmdline is None:
            return True  # sin /proc: confiar en el PID file que escribimos
        return "cxtd" in cmdline

    def _stop_tracked(self, profile_dir: str) -> dict:
        """Para solo el daemon que este gestor trackea por PID file.

        Nunca hace ``pkill`` ni toca un puerto no reclamado. Si el PID fue
        reutilizado por otro proceso, no lo mata: limpia el PID file.
        """
        info = self._read_pid_file(profile_dir)
        if not info:
            return {"stopped": False, "reason": "sin PID file"}
        pid = _coerce_int(info.get("pid"))
        port = _coerce_int(info.get("port"))
        unit = str(info.get("systemd_unit") or "")
        killed = False
        if unit:
            self._systemctl("stop", unit)
            killed = True
        elif pid > 0 and self._alive(pid):
            if self._pid_is_ours(pid):
                log.info("parando cxtd trackeado pid=%s perfil=%s", pid, profile_dir)
                self._kill_impl(pid, signal.SIGTERM)
                deadline = self._mono() + self.stop_timeout
                while self._alive(pid) and self._mono() < deadline:
                    self._sleep(0.1)
                if self._alive(pid):
                    log.warning("cxtd pid=%s no termino con SIGTERM; SIGKILL trackeado", pid)
                    self._kill_impl(pid, signal.SIGKILL)
                    deadline = self._mono() + 2.0
                    while self._alive(pid) and self._mono() < deadline:
                        self._sleep(0.1)
                killed = True
            else:
                log.warning("PID %s reutilizado por otro proceso; no se mata", pid)
        self._clear_pid_file(profile_dir)
        update_port_entry(profile_dir, path=self.ports_path, clear_pid=True)
        if port:
            self._wait_port_closed(port)
        return {"stopped": killed, "pid": pid, "port": port}

    def _wait_port_closed(self, port: int) -> bool:
        deadline = self._mono() + min(self.stop_timeout, 5.0)
        while self._mono() < deadline:
            if not self._port_open_impl(port):
                return True
            self._sleep(0.1)
        log.warning("puerto %s sigue ocupado tras parar el daemon trackeado", port)
        return False

    @staticmethod
    def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, int(port))) == 0

    def _systemctl(self, action: str, unit: str) -> None:
        """``systemctl --user`` solo si ``CXT_P1_SYSTEMD=1`` y hay unidad."""
        if os.environ.get("CXT_P1_SYSTEMD") != "1":
            raise ProfileNotReady(
                "el PID file apunta a una unidad systemd pero CXT_P1_SYSTEMD!=1",
                code="systemd_disabled",
            )
        try:
            result = subprocess.run(
                ["systemctl", "--user", action, unit],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfileNotReady("systemctl --user %s %s fallo: %s" % (action, unit, exc), code="systemd_error") from exc
        if result.returncode != 0:
            raise ProfileNotReady(
                "systemctl --user %s %s devolvio %s: %s" % (action, unit, result.returncode, (result.stderr or "").strip()[:200]),
                code="systemd_error",
            )

    # -- arranque ----------------------------------------------------------

    def _default_probe(self, port: int, token: str = "") -> dict:
        open_ = self._port_open_impl(port)
        if not open_:
            return {"open": False, "health": None, "stat": None}
        health = http_json(port, "/health", token=token, timeout=0.8)
        if health.get("status") == 401:
            return {"open": True, "health": None, "stat": None, "auth_required": True}
        stat = http_json(port, "/stat", token=token, timeout=0.8) if health["ok"] else {"ok": False, "data": {}}
        return {
            "open": True,
            "health": health.get("data") if health["ok"] else None,
            "stat": stat.get("data") if stat.get("ok") else None,
            "auth_required": False,
        }

    def _spawn_daemon(self, profile_dir: str, port: int, cxt_dir: Path) -> dict:
        result = self._spawn(profile_dir, port, cxt_dir)
        if isinstance(result, int):
            return {"pid": int(result), "process": None}
        if not isinstance(result, dict) or not result.get("pid"):
            raise ProfileNotReady("el lanzador no devolvio PID para %s" % profile_dir, code="spawn_failed")
        return result

    def _default_spawn(self, profile_dir: str, port: int, cxt_dir: Path) -> dict:
        if os.environ.get("CXT_P1_SYSTEMD") == "1":
            return self._spawn_systemd(profile_dir)
        binary = self.cxtd_bin
        if not binary or not Path(binary).is_file():
            raise ProfileNotReady(
                "no se encontro el binario cxtd (CXT_DAEMON_BIN/CXT_CXTD o bin/cxtd)",
                code="cxtd_missing",
            )
        cxt_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / ("cxtd-%s.log" % profile_key(profile_dir))
        env = os.environ.copy()
        env["CXT_PORT"] = str(port)
        env["CXT_DIR"] = str(cxt_dir)
        if os.environ.get("CXT_P1_SCHED") == "1":
            env.pop("CXT_NO_SCHED", None)
        else:
            env["CXT_NO_SCHED"] = "1"
        with open(log_path, "ab") as log_fh:
            process = subprocess.Popen(
                [binary],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=env,
                cwd=str(cxt_dir),
                start_new_session=True,
                close_fds=True,
            )
        log.info("cxtd arrancado pid=%s perfil=%s puerto=%s log=%s", process.pid, profile_dir, port, log_path)
        return {"pid": process.pid, "process": process, "log": str(log_path)}

    def _spawn_systemd(self, profile_dir: str) -> dict:
        unit = os.environ.get("CXT_P1_SYSTEMD_UNIT", "").strip()
        if not unit:
            raise ProfileNotReady(
                "CXT_P1_SYSTEMD=1 requiere CXT_P1_SYSTEMD_UNIT (no se asume unidad)",
                code="systemd_unit_missing",
            )
        try:
            check = subprocess.run(
                ["systemctl", "--user", "show", "-p", "LoadState", "--value", unit],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfileNotReady("no se pudo consultar la unidad %s: %s" % (unit, exc), code="systemd_error") from exc
        if check.returncode != 0 or (check.stdout or "").strip() != "loaded":
            raise ProfileNotReady("la unidad systemd %s no existe o no esta cargada" % unit, code="systemd_unit_missing")
        self._systemctl("start", unit)
        pid = 0
        try:
            shown = subprocess.run(
                ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pid = _coerce_int((shown.stdout or "").strip())
        except (OSError, subprocess.TimeoutExpired):
            pid = 0
        log.info("cxtd via systemd unit=%s pid=%s perfil=%s", unit, pid, profile_dir)
        return {"pid": pid, "process": None, "systemd_unit": unit}

    # -- ensure ------------------------------------------------------------

    def ensure_profile_ready(self, profile_dir: str) -> dict:
        """Asegura puerto, ``CXT_DIR`` y daemon listo para un perfil.

        P1: si habia otro perfil activo, lo para (solo si lo arrancamos) y espera
        liberar el puerto. Un trabajo en curso bloquea el cambio con
        :class:`ProfileBusy`. Nunca mata daemons no trackeados.
        """
        self._require_profile(profile_dir)
        cxt_dir = self.profile_cxt_dir(profile_dir)
        cxt_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._manager_lock_path()):
            active = self._load_active()
            active_dir = str(active.get("active_profile_dir") or "")
            if active_dir and active_dir != profile_dir:
                active_port = _coerce_int(active.get("port"))
                if not active.get("managed", False):
                    raise ProfileNotReady(
                        "hay un cxtd no gestionado activo en el puerto %s; no se arranca otro en P1"
                        % active.get("port"),
                        code="untracked_active",
                        active_profile_dir=active_dir,
                    )
                if active_port and not self._read_pid_file(active_dir) and self._port_open_impl(active_port):
                    raise ProfileNotReady(
                        "el perfil activo %s no tiene PID file y su puerto sigue ocupado; no se cambia para no solapar daemons"
                        % active_dir,
                        code="orphan_daemon",
                        active_profile_dir=active_dir,
                    )
                if self._job_in_progress(active_dir):
                    raise ProfileBusy(
                        "hay un trabajo en curso en %s; espera a que termine" % active_dir,
                        active_profile_dir=active_dir,
                    )
                self._stop_tracked(active_dir)
                if active_port and self._port_open_impl(active_port):
                    raise ProfileNotReady(
                        "el puerto %s del perfil anterior sigue ocupado; no se arranca otro daemon" % active_port,
                        code="port_busy",
                        active_profile_dir=active_dir,
                    )
                self._clear_active()
            with _file_lock(cxt_dir / ".lock"):
                return self._ensure_locked(profile_dir, cxt_dir)

    def _ensure_locked(self, profile_dir: str, cxt_dir: Path) -> dict:
        for _attempt in range(4):
            entry = self._ensure_port_entry(profile_dir)
            port = _coerce_int(entry.get("port"))
            status = self._status_for(profile_dir, port)
            if status["port_open"]:
                if status["daemon_alive"]:
                    if status["tracked"] and not status["ready"]:
                        return self._wait_for_ready(profile_dir, port, cxt_dir, None, started=False)
                    self._mark_active(profile_dir, port, status.get("pid"), managed=bool(status["tracked"]))
                    if status["ready"]:
                        return self._payload(profile_dir, port, status, started=False)
                    raise ProfileNotReady(
                        status["status"],
                        profile_dir=profile_dir,
                        port=port,
                        detail=status.get("reason"),
                    )
                if status.get("auth_required"):
                    raise ProfileNotReady(
                        "el puerto %s tiene un cxtd que exige token y no se puede verificar; no se arranca otro"
                        % port,
                        code="auth_mismatch",
                        profile_dir=profile_dir,
                        port=port,
                    )
                # Puerto ocupado por un servicio ajeno: reasignar sin matarlo.
                self._reassign_port(profile_dir, port)
                continue

            spawn = self._spawn_daemon(profile_dir, port, cxt_dir)
            pid = _coerce_int(spawn.get("pid"))
            unit = str(spawn.get("systemd_unit") or "")
            self._write_pid_file(profile_dir, pid=pid, port=port, cxt_dir=cxt_dir, systemd_unit=unit)
            update_port_entry(
                profile_dir,
                path=self.ports_path,
                port=port,
                pid=pid,
                cxt_dir=str(cxt_dir),
                last_started_at=int(time.time()),
            )
            self._mark_active(profile_dir, port, pid, managed=True)
            return self._wait_for_ready(
                profile_dir,
                port,
                cxt_dir,
                spawn.get("process"),
                started=True,
            )
        raise ProfileNotReady("no hay puerto libre para %s" % profile_dir, code="no_port")

    def _wait_for_ready(
        self,
        profile_dir: str,
        port: int,
        cxt_dir: Path,
        process,
        *,
        started: bool,
    ) -> dict:
        deadline = self._mono() + max(self.start_timeout, 0.0)
        last = self._status_for(profile_dir, port)
        while self._mono() < deadline:
            if process is not None and getattr(process, "poll", None) and process.poll() is not None:
                self._clear_pid_file(profile_dir)
                update_port_entry(profile_dir, path=self.ports_path, clear_pid=True)
                self._clear_active()
                log_path = self.logs_dir / ("cxtd-%s.log" % profile_key(profile_dir))
                raise ProfileNotReady(
                    "cxtd termino al arrancar (codigo %s); revisa %s" % (process.returncode, log_path),
                    code="daemon_exited",
                    profile_dir=profile_dir,
                    port=port,
                )
            last = self._status_for(profile_dir, port)
            if last["ready"]:
                return self._payload(profile_dir, port, last, started=started)
            self._sleep(0.2)
        reason = last["status"] if last["daemon_alive"] else "daemon no respondio en %ss" % self.start_timeout
        raise ProfileNotReady(reason, profile_dir=profile_dir, port=port, detail=last.get("reason"))

    # -- enqueue / last_run ------------------------------------------------

    def proxy_autopost(self, profile_dir: str, port: int, payload: dict) -> dict:
        """Reenvia al ``/autopost`` del perfil fijado (nunca a otro puerto)."""
        token = _read_token(self.profile_cxt_dir(profile_dir) / "token") or _read_token(self.home / "token")
        return http_json(port, "/autopost", "POST", payload, token=token, timeout=30.0)

    def enqueue_job(self, profile_dir: str, payload: dict) -> dict:
        status = self.ensure_profile_ready(profile_dir)
        port = _coerce_int(status.get("port"))
        if not port:
            raise ProfileNotReady("el perfil no tiene puerto listo", profile_dir=profile_dir)
        result = self.proxy_autopost(profile_dir, port, payload)
        if not result.get("ok"):
            upstream = result.get("status") or 0
            raise UpstreamError(
                "el daemon del perfil no acepto el trabajo: %s"
                % (result.get("error") or ("HTTP %s" % upstream)),
                status=504 if upstream in (0, 408) else 502,
                profile_dir=profile_dir,
                port=port,
                upstream_status=upstream,
            )
        return {"ok": True, "profile_dir": profile_dir, "port": port, "upstream": result.get("data")}

    def last_run(self, profile_dir: str) -> dict:
        self._require_profile(profile_dir)
        path = self.profile_cxt_dir(profile_dir) / "autopost_status.json"
        fallback = False
        if not path.is_file():
            active = self._load_active()
            active_dir = str(active.get("active_profile_dir") or "").strip()
            is_active = bool(active_dir) and profile_key(active_dir) == profile_key(profile_dir)
            if is_active and not active.get("managed", False):
                shared = self.home / "autopost_status.json"
                if shared.is_file():
                    path = shared
                    fallback = True
        doc = _load_json(path, dict)
        result = {
            "ok": True,
            "profile_dir": profile_dir,
            "last_run": doc.get("last_run"),
            "status_file": str(path),
        }
        if fallback:
            result["fallback"] = True
        return result

    # -- check CLI ---------------------------------------------------------

    def check(self, current_profile_dir: str | None = None) -> dict:
        return self.list_profiles(current_profile_dir)


# ---------------------------------------------------------------------------
# 5. HTTP API
# ---------------------------------------------------------------------------


class ManagerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: ProfileManager) -> None:
        self.manager = manager
        super().__init__(address, ManagerHandler)


class ManagerHandler(BaseHTTPRequestHandler):
    server_version = "CXTProfileManager/" + VERSION
    protocol_version = "HTTP/1.0"

    # -- helpers -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        # Nunca registrar tokens aunque vengan en la query (plan 5.5.3).
        safe_args = tuple(
            re.sub(r"([?&]token=)[^&\s\"]+", r"\1<oculto>", str(arg)) for arg in args
        )
        log.info("%s %s", self.address_string(), fmt % safe_args)

    def _origin(self) -> str:
        return (self.headers.get("Origin") or "").strip()

    def _origin_allowed(self, origin: str) -> bool:
        if not origin:
            return True
        return origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    def _send(self, status: int, payload: dict | None) -> None:
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self._origin()
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-CXT-Token")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.server.manager.token
        if not expected:
            return True
        provided = (self.headers.get("X-CXT-Token") or "").strip()
        auth = (self.headers.get("Authorization") or "").strip()
        if not provided and auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        # Token only via header or Authorization: Bearer; never query strings.
        return bool(provided) and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

    def _read_body(self) -> dict:
        length_raw = self.headers.get("Content-Length")
        if length_raw is None:
            raise _HTTPError(411, "Content-Length requerido", "length_required")
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise _HTTPError(400, "Content-Length invalido", "bad_length") from exc
        if length < 0:
            raise _HTTPError(400, "Content-Length invalido", "bad_length")
        if length > MAX_BODY_BYTES:
            raise _HTTPError(413, "cuerpo demasiado grande", "body_too_large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HTTPError(400, "JSON invalido", "bad_json") from exc
        if not isinstance(data, dict):
            raise _HTTPError(400, "el cuerpo debe ser un objeto JSON", "bad_json")
        return data

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _current_profile(self) -> str:
        query = self._query()
        for key in ("current_profile_dir", "profile_dir"):
            values = query.get(key) or []
            if values:
                return values[0]
        return ""

    # -- dispatch ----------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 (nombre impuesto por stdlib)
        origin = self._origin()
        if not self._origin_allowed(origin):
            self._send(403, {"ok": False, "error": "origen no permitido", "code": "forbidden_origin"})
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-CXT-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            self._route(method)
        except _HTTPError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message, "code": exc.code})
        except ProfileBusy as exc:
            self._send(409, {"ok": False, "error": exc.reason, "code": exc.code, **exc.extra})
        except ProfileNotReady as exc:
            self._send(409, {"ok": False, "error": exc.reason, "code": exc.code, **exc.extra})
        except UpstreamError as exc:
            self._send(exc.status, {"ok": False, "error": exc.reason, "code": "upstream", **exc.extra})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc), "code": "invalid"})
        except Exception:  # frontera HTTP: se registra y se responde 500
            log.exception("error no controlado en %s %s", method, self.path)
            self._send(500, {"ok": False, "error": "error interno", "code": "internal"})

    def _route(self, method: str) -> None:
        origin = self._origin()
        if not self._origin_allowed(origin):
            raise _HTTPError(403, "origen no permitido", "forbidden_origin")
        if not self._authorized():
            raise _HTTPError(401, "token requerido o invalido", "unauthorized")

        path = urlparse(self.path).path.rstrip("/") or "/"
        manager: ProfileManager = self.server.manager

        if method == "GET":
            if path == "/health":
                active = manager._load_active()
                self._send(
                    200,
                    {
                        "ok": True,
                        "service": "profile_manager",
                        "version": VERSION,
                        "pid": os.getpid(),
                        "uptime_s": int(time.time() - manager.started_at),
                        "active_profile_dir": active.get("active_profile_dir") or "",
                        "active_port": active.get("port") or None,
                        "token_required": bool(manager.token),
                    },
                )
                return
            if path == "/profiles":
                self._send(200, manager.list_profiles(self._current_profile() or None))
                return
            if path == "/last_run":
                profile_dir = self._current_profile()
                if not profile_dir:
                    raise _HTTPError(400, "falta profile_dir", "missing_profile")
                self._send(200, manager.last_run(profile_dir))
                return
            raise _HTTPError(404, "ruta no encontrada", "not_found")

        if method == "POST":
            if path == "/select":
                body = self._read_body()
                profile_dir = body.get("profile_dir")
                if not isinstance(profile_dir, str) or not profile_dir:
                    raise _HTTPError(400, "falta profile_dir", "missing_profile")
                self._send(200, manager.ensure_profile_ready(profile_dir))
                return
            if path == "/enqueue":
                body = self._read_body()
                profile_dir = body.get("profile_dir")
                if not isinstance(profile_dir, str) or not profile_dir:
                    raise _HTTPError(400, "falta profile_dir", "missing_profile")
                payload = {key: value for key, value in body.items() if key != "profile_dir"}
                self._send(200, manager.enqueue_job(profile_dir, payload))
                return
            raise _HTTPError(404, "ruta no encontrada", "not_found")

        raise _HTTPError(405, "metodo no permitido", "method_not_allowed")


# ---------------------------------------------------------------------------
# 6. main()
# ---------------------------------------------------------------------------


def _configure_logging(home: Path, verbose: bool = False) -> None:
    """stderr + ``~/.cxt/logs/profile_manager.log`` (UTF-8, sin rotacion)."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    log_dir = home / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "profile_manager.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        log.warning("no se pudo abrir el log en %s: %s", log_dir, exc)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gestor de perfiles CXT (P1)")
    parser.add_argument("--host", default="127.0.0.1", help="interfaz (solo loopback)")
    parser.add_argument("--port", type=int, default=DEFAULT_MANAGER_PORT, help="puerto del gestor")
    parser.add_argument("--check", action="store_true", help="descubre y muestra perfiles; no sirve HTTP")
    parser.add_argument("--once", action="store_true", help="atiende una sola peticion HTTP y sale")
    parser.add_argument("--profile", default="", help="profile_dir actual para --check")
    parser.add_argument("--verbose", action="store_true", help="log DEBUG")
    args = parser.parse_args(argv)

    if args.host not in LOOPBACK_HOSTS:
        print("error: el gestor solo puede escuchar en loopback (%s)" % ", ".join(sorted(LOOPBACK_HOSTS)), file=sys.stderr)
        return 2

    try:
        manager = ProfileManager()
    except TokenReadError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    _configure_logging(manager.home, verbose=args.verbose)

    if args.check:
        result = manager.check(args.profile or None)
        _print_json(result)
        return 0 if result.get("ok") else 1

    try:
        server = ManagerHTTPServer((args.host, args.port), manager)
    except OSError as exc:
        log.error("no se pudo escuchar en %s:%s: %s", args.host, args.port, exc)
        return 1

    def _shutdown(_signum, _frame) -> None:
        log.info("senal recibida; cerrando gestor")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(
        "profile_manager %s en http://%s:%s (home=%s, token=%s)",
        VERSION,
        args.host,
        args.port,
        manager.home,
        "si" if manager.token else "no",
    )
    if not manager.token:
        log.info("sin %s: token no exigido (solo loopback + origen de extension)",
                 manager.home / "profile_manager.token")
    try:
        if args.once:
            server.handle_request()
        else:
            server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        log.info("gestor detenido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
