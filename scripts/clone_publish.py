import json, os, subprocess, sys, time

import cxtlib

CXT = cxtlib.resolve_cxt()
TAB = cxtlib.resolve_tab(CXT)


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


COMPOSER_JS = r'''(()=>{function scan(root){let best=null,bd=1e9;for(const e of root.querySelectorAll('*')){const t=((e.innerText||'')+'').toLowerCase();if(t.indexOf('estás pensando')<0||t.length>90)continue;const r=e.getBoundingClientRect();if(r.width>=120&&r.height>=18&&r.height<=140){const a=r.width*r.height;if(a<bd){bd=a;best=e;}}}return best;}let best=scan(document);if(!best){let n=0;const roots=[];for(const e of document.querySelectorAll('*')){if(e.shadowRoot){roots.push(e.shadowRoot);if(++n>200)break;}}for(const sr of roots){best=scan(sr);if(best)break;}}if(!best)return 'none';best.scrollIntoView({block:'center'});const r=best.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()'''


def cdp(expr):
    r = call("cdp", {"method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}, "tabId": TAB})
    return r.get("result", {}).get("result", {}).get("value")


def cur_url():
    u = cdp("location.href")
    return u if isinstance(u, str) else ""


def ready():
    return cdp("document.readyState") == "complete"


def ensure_fresh():
    u = cur_url()
    if not u or "facebook.com" not in u:
        u = os.environ.get("CXT_POST_URL") or "https://www.facebook.com/"
    log("navego a %s" % u[:60])
    call("navigate", {"tabId": TAB, "url": u, "timeout": 30000})
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
    return call("cdpclick", {"x": x, "y": y, "tabId": TAB})


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
call("tab.activate", {"tabId": TAB})
ensure_fresh()
call("cdpkey", {"key": "Escape", "tabId": TAB})
time.sleep(0.3)

c = find_composer()
if not c:
    log("no encontre el compositor")
    print(json.dumps({"ok": False, "stage": "composer-not-found"}))
    sys.exit(1)
log("compositor en %s,%s" % (c["x"], c["y"]))
click(c["x"] + c["w"] // 2, c["y"] + c["h"] // 2)
s = wait_for("siguiente", tries=25, interval=0.3, minw=60, lo=20, hi=90, exact=True)
if not s:
    log("no aparecio 'Siguiente'")
    print(json.dumps({"ok": False, "stage": "siguiente"}))
    sys.exit(1)

typed = call("type", {"value": text, "tabId": TAB}).get("typed")
if not typed:
    for _ in range(2):
        if call("cdptype", {"text": text, "tabId": TAB}).get("typed"):
            typed = True
            break
log("texto escrito" if typed else "AVISO: no confirme el texto")

s2 = wait_for("siguiente", tries=12, interval=0.25, minw=60, lo=20, hi=90, exact=True) or s
click(s2["x"] + s2["w"] // 2, s2["y"] + s2["h"] // 2)
log("pulse Siguiente")

pub = wait_for("publicar", tries=75, interval=0.4, minw=80, lo=20, hi=70, exact=True)
if not pub:
    s3 = wait_for("siguiente", tries=6, interval=0.25, minw=60, lo=20, hi=90, exact=True)
    if s3:
        click(s3["x"] + s3["w"] // 2, s3["y"] + s3["h"] // 2)
        log("reintente Siguiente")
        pub = wait_for("publicar", tries=40, interval=0.4, minw=80, lo=20, hi=70, exact=True)
if not pub:
    log("no aparecio el boton Publicar")
    print(json.dumps({"ok": False, "stage": "publicar"}))
    sys.exit(1)
log("boton Publicar en %s,%s" % (pub["x"], pub["y"]))
if "--dry" in sys.argv:
    print(json.dumps({"ok": True, "dry": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]], "ms": int((time.time() - t0) * 1000)}, ensure_ascii=False))
    sys.exit(0)
click(pub["x"] + pub["w"] // 2, pub["y"] + 8)
log("pulse Publicar")

ok = False
for _ in range(30):
    time.sleep(0.4)
    if not pick("publicar", 80, 20, 70, exact=True):
        ok = True
        break
log("resultado: " + ("publicado" if ok else "no confirme el cierre del panel"))
print(json.dumps({"ok": ok, "ms": int((time.time() - t0) * 1000)}, ensure_ascii=False))
