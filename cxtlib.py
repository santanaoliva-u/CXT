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


AUTO_TAB_MARK = "#cxt-auto"


def _tab_id(t):
    """Id de una pestana venga como `id` (lista `tabs`) o `tabId` (ops nuevas)."""
    return t.get("id") or t.get("tabId") if isinstance(t, dict) else None


def _is_auto_tab(t):
    """True si la extension marco la pestana como propia de automatizacion.

    `tab.ensure_auto` puede exponer la marca como flag (`cxtAuto`) o como hash en
    la URL (`#cxt-auto`). Solo es una PREFERENCIA al resolver: nunca activa nada."""
    if not isinstance(t, dict):
        return False
    if t.get("cxtAuto") or t.get("cxt_auto"):
        return True
    return AUTO_TAB_MARK in (t.get("url") or "")


def resolve_tab(cxt=None, url_contains=DEFAULT_TAB_URL):
    """CXT_TAB si es entero; si no, la pestana marcada; si no, la primera de Facebook.

    Solo resuelve un id (compatibilidad con los drivers viejos): no activa, no
    navega y no roba foco. La pestana marcada es la que crea `tab.ensure_auto`."""
    env = os.environ.get("CXT_TAB")
    if env:
        try:
            return int(env)
        except ValueError:
            pass  # no numerico (p. ej. "auto"): caemos al descubrimiento por marca
    cxt = cxt or resolve_cxt()
    tabs = _tabs(cxt)
    for t in tabs:
        if _is_auto_tab(t):
            return _tab_id(t)
    for t in tabs:
        if url_contains in (t.get("url") or ""):
            return _tab_id(t)
    return None


def ensure_background_tab(cxt=None, url=None):
    """Pide a la extension su pestana/ventana propia (sin foco) y devuelve el id.

    Usa `tab.ensure_auto`, que crea o reutiliza una ventana no enfocada. Si la
    extension es antigua y la op no existe, falla con RuntimeError: NUNCA se cae
    a `tab.activate` (eso robaria el foco del usuario)."""
    cxt = cxt or resolve_cxt()
    r = call(cxt, "tab.ensure_auto", {"url": url or "https://www.facebook.com/"})
    tab = _tab_id(r) if isinstance(r, dict) else None
    if tab is None:
        detalle = ""
        if isinstance(r, dict):
            detalle = r.get("error") or r.get("_err") or r.get("_raw") or ""
        raise RuntimeError(
            "tab.ensure_auto no disponible (extension antigua o sin permiso): %s"
            % (detalle or r))
    return tab


def activate_tab(cxt=None, tab=None):
    """Activa una pestana DE VERDAD (foco del sistema). Solo uso explicito/CLI.

    La automatizacion no debe llamar a esta funcion: usa `ensure_background_tab`."""
    cxt = cxt or resolve_cxt()
    if tab is None:
        tab = resolve_tab(cxt)
    if tab is None:
        raise SystemExit("no encontre pestana de Facebook (set CXT_TAB)")
    call(cxt, "tab.activate", {"tabId": tab})
    return tab


def ensure_tab(cxt=None, tab=None, url_contains=DEFAULT_TAB_URL, activate=False):
    """Resuelve la pestana objetivo SIN activarla (modo automatizacion).

    `activate=True` es solo para uso explicito del usuario/CLI; ningun driver de
    automatizacion debe pasarlo. Se mantiene la firma historica por compatibilidad."""
    cxt = cxt or resolve_cxt()
    if tab is None:
        tab = resolve_tab(cxt, url_contains)
    if tab is None:
        raise SystemExit("no encontre pestana de Facebook (set CXT_TAB)")
    if activate:
        return activate_tab(cxt, tab)
    return tab


def cdp(cxt, tab, expr, timeout=90, await_promise=True):
    r = call(cxt, "cdp", {"tabId": tab, "method": "Runtime.evaluate",
                          "params": {"expression": expr, "returnByValue": True,
                                     "awaitPromise": bool(await_promise)}}, timeout)
    try:
        return r["result"]["result"].get("value")
    except Exception:
        return None


_UI_OFF = ("(()=>{let s=document.getElementById('cxt-auto-suppress');"
           "if(!s){s=document.createElement('style');s.id='cxt-auto-suppress';"
           "s.textContent='[data-cxt-auto-ui]{pointer-events:none!important}';"
           "document.documentElement.appendChild(s);}return true;})()")
_UI_ON = ("(()=>{const s=document.getElementById('cxt-auto-suppress');"
          "if(s)s.remove();return true;})()")


def click(cxt, tab, x, y, timeout=120):
    """Click CDP con `buttons` (el `cdpclick` viejo no lo mandaba y FB lo ignora).

    Suprime la barra flotante de CXT solo durante el click: si el usuario la
    arrastro sobre el compositor, sus clicks se perderian.
    """
    x, y = int(x), int(y)
    call(cxt, "cdp", {"tabId": tab, "method": "Runtime.evaluate",
                      "params": {"expression": _UI_OFF, "returnByValue": True}}, timeout)
    try:
        call(cxt, "cdp", {"tabId": tab, "method": "Input.dispatchMouseEvent",
                          "params": {"type": "mouseMoved", "x": x, "y": y,
                                     "button": "none", "buttons": 0}}, timeout)
        call(cxt, "cdp", {"tabId": tab, "method": "Input.dispatchMouseEvent",
                          "params": {"type": "mousePressed", "x": x, "y": y,
                                     "button": "left", "buttons": 1, "clickCount": 1}}, timeout)
        time.sleep(0.08)
        call(cxt, "cdp", {"tabId": tab, "method": "Input.dispatchMouseEvent",
                          "params": {"type": "mouseReleased", "x": x, "y": y,
                                     "button": "left", "buttons": 1, "clickCount": 1}}, timeout)
    finally:
        call(cxt, "cdp", {"tabId": tab, "method": "Runtime.evaluate",
                          "params": {"expression": _UI_ON, "returnByValue": True}}, timeout)
    return {"clicked": True, "x": x, "y": y}


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
