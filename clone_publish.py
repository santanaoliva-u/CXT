import atexit, json, os, subprocess, sys, time

import cxtlib

CXT = cxtlib.resolve_cxt()
TAB = cxtlib.resolve_tab(CXT)
if not TAB:
    sys.exit("no encontre pestana de Facebook (abre Facebook o define CXT_TAB)")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def call(op, args):
    p = subprocess.run([CXT, op, json.dumps(args)], capture_output=True, text=True, timeout=180)
    try:
        return json.loads(p.stdout.strip())
    except Exception:
        return {"_raw": p.stdout.strip(), "_err": p.stderr.strip()}


def deep(text, limit=1500):
    d = call("deepfind", {"text": text, "selector": "*", "visibleOnly": True, "limit": limit, "tabId": TAB})
    return d.get("items") or []


def pick(text, minw=80, lo=18, hi=110, exact=False):
    items = [x for x in deep(text) if x.get("w", 0) >= minw and lo <= x.get("h", 0) <= hi]
    if exact:
        items = [x for x in items if x.get("text", "").strip().lower() == text.lower()]
    items.sort(key=lambda x: -(x["w"] * x["h"]))
    return items[0] if items else None


COMPOSER_JS = r'''(()=>{function scan(root){let best=null,bd=1e9;for(const e of root.querySelectorAll('div,span,button,[role],[aria-label]')){const a=(e.getAttribute('aria-label')||'').toLowerCase();const t=a.indexOf('pensando')>=0?'pensando':((e.textContent||'')+'').toLowerCase().slice(0,300);if(t.indexOf('pensando')<0)continue;const r=e.getBoundingClientRect();if(r.width>=120&&r.height>=18&&r.height<=140){const ar=r.width*r.height;if(ar<bd){bd=ar;best=e;}}}return best;}let best=scan(document);if(!best){let n=0;for(const e of document.querySelectorAll('*')){if(e.shadowRoot){best=scan(e.shadowRoot);if(best)break;if(++n>200)break;}}}if(!best)return 'none';best.scrollIntoView({block:'center'});const r=best.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()'''
EDITOR_JS = r'''(()=>{let best=null,bd=1e9;for(const e of document.querySelectorAll('[contenteditable="true"]')){const r=e.getBoundingClientRect();if(r.width<200||r.height<10)continue;let v=false;try{v=e.checkVisibility?e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}):true;}catch(x){}if(!v)continue;const t=((e.getAttribute('aria-label')||'')+' '+(e.textContent||'')).toLowerCase();if(t.indexOf('pensando')<0&&t.indexOf('escribe algo')<0)continue;const a=r.width*r.height;if(a<bd){bd=a;best=e;}}if(!best)return 'none';best.scrollIntoView({block:'center'});const r2=best.getBoundingClientRect();return {x:r2.x,y:r2.y,w:r2.width,h:r2.height};})()'''


def cdp(expr):
    r = call("cdp", {"method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}, "tabId": TAB})
    return r.get("result", {}).get("result", {}).get("value")


CXT_UI_OFF = r"""(()=>{let s=document.getElementById('cxt-auto-suppress');if(!s){s=document.createElement('style');s.id='cxt-auto-suppress';s.textContent='[data-cxt-auto-ui]{pointer-events:none!important}';document.documentElement.appendChild(s);}return true;})()"""
CXT_UI_ON = r"""(()=>{const s=document.getElementById('cxt-auto-suppress');if(s)s.remove();return true;})()"""


def cxt_ui(enabled):
    """La barra flotante de CXT puede quedar encima del compositor; no debe interceptar."""
    try:
        cdp(CXT_UI_ON if enabled else CXT_UI_OFF)
        return True
    except Exception as exc:
        log("cxt_ui error: %s" % exc)
        return False


atexit.register(lambda: cxt_ui(True))


def cur_url():
    u = cdp("location.href")
    return u if isinstance(u, str) else ""


def ready():
    return cdp("document.readyState") == "complete"


def clear_drafts():
    try:
        call("cdpkey", {"key": "Escape", "tabId": TAB})
        cdp(
            "(function(){var es=document.querySelectorAll('[contenteditable=true]');"
            "var n=0;for(var i=0;i<es.length;i++){var e=es[i];var r=e.getBoundingClientRect();"
            "if(r.width<10||r.height<8)continue;try{e.focus();document.execCommand('selectAll');"
            "document.execCommand('delete');}catch(x){}e.innerHTML='';n++;}return n;})()"
        )
        return True
    except Exception as exc:
        log("clear_drafts error: %s" % exc)
        return False


def ensure_fresh():
    want = os.environ.get("CXT_POST_URL") or "https://www.facebook.com/"
    log("navego a %s" % want[:60])
    call("navigate", {"tabId": TAB, "url": want, "timeout": 30000})
    for _ in range(25):
        time.sleep(0.6)
        if ready():
            return True
    return False


def find_composer(tries=25):
    for _ in range(3):
        r = call("scroll", {"x": 0, "y": -200000, "tabId": TAB})
        time.sleep(0.3)
        if r.get("y", 1) == 0:
            break
    for _ in range(tries):
        if not ready():
            time.sleep(0.6)
            continue
        v = cdp(COMPOSER_JS)
        if isinstance(v, dict) and v.get("w", 0) >= 120:
            return v
        call("scroll", {"x": 0, "y": 300, "tabId": TAB})
        time.sleep(0.7)
    return None


def click(x, y):
    # Click CDP completo. `cdpclick` del extension no manda `buttons` y el
    # compositor de Facebook lo ignora. Suprimimos la barra flotante de CXT
    # justo antes de cada click: la pagina puede borrar el <style> al re-renderizar.
    cxt_ui(False)
    call("cdp", {"method": "Input.dispatchMouseEvent", "params": {
        "type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0}, "tabId": TAB})
    call("cdp", {"method": "Input.dispatchMouseEvent", "params": {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1}, "tabId": TAB})
    time.sleep(0.08)
    call("cdp", {"method": "Input.dispatchMouseEvent", "params": {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1}, "tabId": TAB})
    cxt_ui(True)
    return {"clicked": True, "x": x, "y": y}


def js_click_composer(c):
    """Fallback: click sintetico sobre el boton real del compositor (ignora overlays)."""
    x = int(c["x"] + max(20, min(80, int(c["w"]) // 4)))
    y = int(c["y"] + c["h"] // 2)
    expr = """(()=>{const x=%d,y=%d;const bs=[...document.querySelectorAll('[role=button]')];
      for(const e of bs){const r=e.getBoundingClientRect();
        if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom&&/pensando/i.test(e.innerText||'')){
          e.click();return true;}}
      const e=document.elementFromPoint(x,y);const b=e&&e.closest('[role=button]');
      if(b){b.click();return true;}return false;})()""" % (x, y)
    return cdp(expr)


def js_click_text(text):
    expr = """(()=>{const t=%s;for(const e of document.querySelectorAll('[role=button],button')){
      const r=e.getBoundingClientRect();const s=(e.innerText||'').trim().toLowerCase();
      if(r.width>30&&r.height>15&&s===t){e.click();return true;}}return false;})()""" % json.dumps(text.lower())
    return cdp(expr)


def wait_for(text, tries=30, interval=0.3, minw=60, lo=18, hi=90, exact=False):
    for _ in range(tries):
        p = pick(text, minw, lo, hi, exact)
        if p:
            return p
        time.sleep(interval)
    return None


with open(sys.argv[1], encoding="utf-8") as f:
    text = f.read().strip()
assert text, "post vacio"

t0 = time.time()
log("=== PUBLICAR (clone) ===")
# Sin activacion: se publica en la pestana resuelta (propia/segundo plano) para
# no robar el foco del usuario.
if not clear_drafts():
    cxt_ui(True)
    print(json.dumps({"ok": False, "stage": "clear-drafts-failed", "error": "no se pudieron limpiar drafts"}))
    sys.exit(1)
ensure_fresh()
cxt_ui(False)
call("cdpkey", {"key": "Escape", "tabId": TAB})
time.sleep(0.3)

c = find_composer()
if not c:
    log("no encontre el compositor")
    cxt_ui(True)
    print(json.dumps({"ok": False, "stage": "composer-not-found"}))
    sys.exit(1)
log("compositor en %s,%s" % (c["x"], c["y"]))
# El centro del texto del compositor no siempre recibe el click nativo; el
# tercio izquierdo (zona del avatar) sí. Evitamos tambien la barra flotante.
click(c["x"] + max(20, min(80, int(c["w"]) // 4)), c["y"] + c["h"] // 2)

# El modal de Facebook tiene 2 variantes: directa (boton Publicar a la vista)
# o de 2 pasos (boton Siguiente -> panel -> Publicar). Aceptamos las dos.
opened = None
for _i in range(25):
    time.sleep(0.4)
    if pick("publicar", 80, 20, 70, exact=True):
        opened = "publicar"
        break
    if pick("siguiente", 60, 20, 90, exact=True):
        opened = "siguiente"
        break
    if _i == 4:
        log("click nativo no abrio; fallback JS sobre el compositor")
        js_click_composer(c)
if not opened:
    log("el modal no abrio (ni Publicar ni Siguiente)")
    cxt_ui(True)
    print(json.dumps({"ok": False, "stage": "modal"}))
    sys.exit(1)
log("modal abierto (variante %s)" % opened)

# enfocar el editor del modal (evita que el texto caiga en una caja de comentarios)
ed = cdp(EDITOR_JS)
if isinstance(ed, dict):
    click(ed["x"] + ed["w"] // 2, ed["y"] + ed["h"] // 2)
    time.sleep(0.3)

frag = text.strip().split("\n")[0][:18]
typed = False
for _ in range(3):
    if not call("type", {"value": text, "tabId": TAB}).get("typed"):
        call("cdptype", {"text": text, "tabId": TAB})
    time.sleep(0.4)
    if cdp("(()=>{const a=document.activeElement;return !!a&&a.isContentEditable&&(a.innerText||'').indexOf(" + json.dumps(frag) + ")>=0;})()"):
        typed = True
        break
log("texto escrito" if typed else "AVISO: no confirme el texto")

pub = None
for attempt in range(5):
    pub = wait_for("publicar", tries=20, interval=0.4, minw=80, lo=20, hi=70, exact=True)
    if pub:
        break
    s3 = wait_for("siguiente", tries=6, interval=0.3, minw=60, lo=20, hi=90, exact=True)
    if s3:
        click(s3["x"] + s3["w"] // 2, s3["y"] + s3["h"] // 2)
        log("pulse Siguiente (intento %d)" % (attempt + 1))
    else:
        log("aun sin Publicar (intento %d)" % (attempt + 1))
if not pub:
    log("no aparecio el boton Publicar")
    cxt_ui(True)
    print(json.dumps({"ok": False, "stage": "publicar"}))
    sys.exit(1)
log("boton Publicar en %s,%s" % (pub["x"], pub["y"]))
if "--dry" in sys.argv:
    cxt_ui(True)
    print(json.dumps({"ok": True, "dry": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]], "ms": int((time.time() - t0) * 1000)}, ensure_ascii=False))
    sys.exit(0)
click(pub["x"] + pub["w"] // 2, pub["y"] + 8)
log("pulse Publicar")

ok = False
for _i in range(30):
    time.sleep(0.4)
    if not pick("publicar", 80, 20, 70, exact=True):
        ok = True
        break
    if _i == 4:
        log("click nativo no cerro el panel; fallback JS sobre Publicar")
        js_click_text("Publicar")
log("resultado: " + ("publicado" if ok else "no confirme el cierre del panel"))
cxt_ui(True)
print(json.dumps({"ok": ok, "ms": int((time.time() - t0) * 1000)}, ensure_ascii=False))
