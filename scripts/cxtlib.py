"""Helpers compartidos por los scripts de automatizacion CXT.

Cada script resuelve su binario, su tab y sus rutas de forma explicita (o con un
`caller` ligado), sin repetir codigo. Respeta CXT_BIN, CXT_TAB y CXT_DIR (mismos
overrides que el daemon).
"""
import functools
import json
import os
import subprocess
import time
import unicodedata

DEFAULT_TAB_URL = "facebook.com"


def here():
    return os.path.dirname(os.path.abspath(__file__))


def resolve_dir():
    """Carpeta de estado/scripts del daemon (CXT_DIR; por defecto ~/.cxt)."""
    return os.environ.get("CXT_DIR") or os.path.join(os.path.expanduser("~"), ".cxt")


def resolve_cxt():
    """Ruta al CLI cxt: CXT_BIN, o <repo>/bin/cxt, o $CXT_DIR/bin/cxt."""
    env = os.environ.get("CXT_BIN")
    if env:
        return env
    local = here()
    for cand in (os.path.join(local, "bin", "cxt"),
                 os.path.normpath(os.path.join(local, os.pardir, "bin", "cxt")),
                 os.path.join(resolve_dir(), "bin", "cxt")):
        if os.path.exists(cand):
            return cand
    return os.path.join(resolve_dir(), "bin", "cxt")


def resolve_script(name):
    """Ruta a un script hermano (news.sh, clone_publish.py...) segun CXT_DIR."""
    for base in (here(), resolve_dir()):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return os.path.join(resolve_dir(), name)


def call(cxt, op, args=None, timeout=120):
    """Ejecuta `cxt <op> <json>` y devuelve el JSON; nunca lanza por timeout."""
    try:
        p = subprocess.run([cxt, op, json.dumps(args or {})],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "_err": "timeout tras %ss en op %s" % (timeout, op)}
    except OSError as e:
        return {"ok": False, "_err": "no se pudo ejecutar %s: %s" % (cxt, e)}
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "_raw": out, "_err": (p.stderr or "").strip()}


def caller(cxt, timeout=120):
    """Devuelve un `call(op, args)` ligado al binario y a un timeout por defecto.

    Evita que cada script repita el binario/tiempo en cada llamada."""
    return functools.partial(call, cxt, timeout=timeout)


def _tabs(cxt):
    r = call(cxt, "tabs", {})
    if isinstance(r, list):
        return r
    if isinstance(r, dict) and isinstance(r.get("tabs"), list):
        return r["tabs"]
    return []


def resolve_tab(cxt=None, url_contains=DEFAULT_TAB_URL):
    """CXT_TAB si es un entero; si no, el primer tab que contenga url_contains."""
    env = os.environ.get("CXT_TAB")
    if env:
        try:
            return int(env)
        except ValueError:
            pass  # no numerico: caemos al descubrimiento por url
    cxt = cxt or resolve_cxt()
    for t in _tabs(cxt):
        if url_contains in (t.get("url") or ""):
            return t.get("id") or t.get("tabId")
    return None


def ensure_tab(cxt=None, tab=None, url_contains=DEFAULT_TAB_URL):
    """Resuelve (si hace falta) y activa la pestana objetivo; devuelve su id."""
    cxt = cxt or resolve_cxt()
    if tab is None:
        tab = resolve_tab(cxt, url_contains)
    if tab is None:
        raise SystemExit("no encontre pestana de Facebook (set CXT_TAB)")
    call(cxt, "tab.activate", {"tabId": tab})
    return tab


def cdp(cxt, tab, expr, timeout=90, await_promise=True):
    r = call(cxt, "cdp", {"tabId": tab, "method": "Runtime.evaluate",
                          "params": {"expression": expr, "returnByValue": True,
                                     "awaitPromise": bool(await_promise)}}, timeout)
    try:
        return r["result"]["result"].get("value")
    except Exception:
        return None


def click(cxt, tab, x, y, timeout=120):
    return call(cxt, "cdpclick", {"tabId": tab, "x": int(x), "y": int(y)}, timeout)


def clear_draft(cxt, tab):
    """Limpia cajas contenteditable y cierra menus/dialogos de JS pendientes."""
    call(cxt, "cdpkey", {"key": "Escape", "tabId": tab})
    call(cxt, "cdp", {"method": "Page.handleJavaScriptDialog",
                      "params": {"accept": True}, "tabId": tab})
    expr = ("(()=>{const s=document.querySelectorAll('[contenteditable=true]');"
            "for(const e of s){const r=e.getBoundingClientRect();"
            "if(r.width<10||r.height<8)continue;"
            "try{e.focus();document.execCommand('selectAll');document.execCommand('delete');}catch(_){}"
            "e.innerHTML='';try{e.dispatchEvent(new InputEvent('input',{bubbles:true}));}catch(_){}}"
            "if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();"
            "return s.length;})()")
    return cdp(cxt, tab, expr, await_promise=False)


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


def write_json_atomic(path, obj, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def write_status(path, d):
    """Escritura atomica del estado de un job; registra el fallo, no lo traga."""
    try:
        write_json_atomic(path, d)
    except Exception as e:
        print("write_status err %s: %s" % (path, e), flush=True)


def deep(cxt, tab, text, limit=800, timeout=180):
    d = call(cxt, "deepfind", {"text": text, "selector": "*", "visibleOnly": True,
                               "limit": limit, "tabId": tab}, timeout)
    return d.get("items") or []


def pick(cxt, tab, text, minw=80, lo=18, hi=110, exact=False, limit=800, timeout=180):
    items = [x for x in deep(cxt, tab, text, limit=limit, timeout=timeout)
             if x.get("w", 0) >= minw and lo <= x.get("h", 0) <= hi]
    if exact:
        items = [x for x in items if x.get("text", "").strip().lower() == text.lower()]
    items.sort(key=lambda x: -(x["w"] * x["h"]))
    return items[0] if items else None


def wait_for(cxt, tab, text, tries=30, interval=0.4, minw=80, lo=18, hi=110,
             exact=False, limit=800, timeout=180):
    for _ in range(tries):
        p = pick(cxt, tab, text, minw, lo, hi, exact, limit=limit, timeout=timeout)
        if p:
            return p
        time.sleep(interval)
    return None
