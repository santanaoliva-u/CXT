"""Helpers compartidos por los scripts de automatizacion CXT.

Sin estado global: cada script resuelve su binario, su tab y sus rutas de forma
explicita. Respeta CXT_BIN, CXT_TAB y CXT_DIR (mismo override que el daemon).
"""
import json
import os
import subprocess
import unicodedata


def here():
    return os.path.dirname(os.path.abspath(__file__))


def resolve_cxt():
    return os.environ.get("CXT_BIN") or os.path.join(here(), "bin", "cxt")


def call(cxt, op, args=None, timeout=120):
    p = subprocess.run([cxt, op, json.dumps(args or {})],
                       capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "_raw": out, "_err": (p.stderr or "").strip()}


def _tabs(cxt):
    try:
        r = call(cxt, "tabs", {})
    except Exception:
        return []
    if isinstance(r, list):
        return r
    if isinstance(r, dict) and isinstance(r.get("tabs"), list):
        return r["tabs"]
    return []


def resolve_tab(cxt=None, url_contains="facebook.com"):
    """CXT_TAB si esta definido; si no, el primer tab que contenga url_contains."""
    env = os.environ.get("CXT_TAB")
    if env:
        try:
            return int(env)
        except ValueError:
            return None
    cxt = cxt or resolve_cxt()
    for t in _tabs(cxt):
        if url_contains in (t.get("url") or ""):
            return t.get("id") or t.get("tabId")
    return None


def norm(s):
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)
