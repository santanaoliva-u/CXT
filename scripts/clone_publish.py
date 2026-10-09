import json, os, subprocess, sys, time

import cxtlib

CXT = cxtlib.resolve_cxt()
TAB = cxtlib.resolve_tab(CXT)


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


def find_composer(max_steps=40, step=220, want_y=260):
    for _ in range(3):
        r = call("scroll", {"x": 0, "y": -200000, "tabId": TAB})
        time.sleep(0.35)
        if r.get("y", 1) == 0:
            break
    time.sleep(0.4)
    for _ in range(max_steps):
        p = pick("estás pensando", 120, 20, 120) or pick("estás pensando", 100, 15, 200)
        if p:
            dy = p["y"] - want_y
            if abs(dy) > 40:
                call("scroll", {"x": 0, "y": dy, "tabId": TAB})
                time.sleep(0.3)
                p = pick("estás pensando", 120, 20, 120) or p
            return p
        call("scroll", {"x": 0, "y": step, "tabId": TAB})
        time.sleep(0.28)
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


text = open(sys.argv[1], encoding="utf-8").read().strip()
assert text, "post vacio"

t0 = time.time()
call("tab.activate", {"tabId": TAB})
call("cdpkey", {"key": "Escape", "tabId": TAB})
time.sleep(0.25)

s = wait_for("siguiente", tries=1, interval=0.1, minw=60, lo=20, hi=90, exact=True)
if s:
    call("type", {"value": text, "tabId": TAB})
else:
    c = find_composer()
    if not c:
        print(json.dumps({"ok": False, "stage": "composer-not-found"}))
        sys.exit(1)
    click(c["x"] + c["w"] // 2, c["y"] + c["h"] // 2)
    s = wait_for("siguiente", tries=25, interval=0.3, minw=60, lo=20, hi=90, exact=True)
    if not s:
        print(json.dumps({"ok": False, "stage": "siguiente"}))
        sys.exit(1)
    if not call("type", {"value": text, "tabId": TAB}).get("typed"):
        for _ in range(2):
            if call("cdptype", {"text": text, "tabId": TAB}).get("typed"):
                break
click(s["x"] + s["w"] // 2, s["y"] + s["h"] // 2)

pub = wait_for("publicar", tries=40, interval=0.35, minw=80, lo=20, hi=70, exact=True)
if not pub:
    print(json.dumps({"ok": False, "stage": "publicar"}))
    sys.exit(1)
if "--dry" in sys.argv:
    print(json.dumps({"ok": True, "dry": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]], "ms": int((time.time() - t0) * 1000)}, ensure_ascii=False))
    sys.exit(0)
click(pub["x"] + pub["w"] // 2, pub["y"] + 8)

ok = False
for _ in range(25):
    time.sleep(0.4)
    if not pick("publicar", 80, 20, 70, exact=True):
        ok = True
        break

print(json.dumps({"ok": ok, "ms": int((time.time() - t0) * 1000)}, ensure_ascii=False))
