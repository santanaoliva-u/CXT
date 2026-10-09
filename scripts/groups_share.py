import json, os, subprocess, sys, time, unicodedata

import cxtlib

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = cxtlib.resolve_cxt()
TAB = cxtlib.resolve_tab(CXT)
GJ = os.path.join(HERE, "groups.json")
STATUS = os.path.join(HERE, "share_status.json")
PID = os.path.join(HERE, "share.pid")


def call(op, args, timeout=180):
    p = subprocess.run([CXT, op, json.dumps(args)], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip())
    except Exception:
        return {"_raw": p.stdout.strip(), "_err": p.stderr.strip()}


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def deep(text, limit=800):
    d = call("deepfind", {"text": text, "selector": "*", "visibleOnly": True, "limit": limit, "tabId": TAB})
    return d.get("items") or []


def pick(text, minw=80, lo=18, hi=110, exact=False):
    items = [x for x in deep(text) if x.get("w", 0) >= minw and lo <= x.get("h", 0) <= hi]
    if exact:
        items = [x for x in items if x.get("text", "").strip().lower() == text.lower()]
    items.sort(key=lambda x: -(x["w"] * x["h"]))
    return items[0] if items else None


def wait_for(text, tries=30, interval=0.4, minw=80, lo=18, hi=110, exact=False):
    for _ in range(tries):
        p = pick(text, minw, lo, hi, exact)
        if p:
            return p
        time.sleep(interval)
    return None


def ensure_groups():
    if os.path.exists(GJ) and os.path.getsize(GJ) > 2:
        return
    scan = os.path.join(HERE, "groups_scan.py")
    if os.path.exists(scan):
        subprocess.run([sys.executable, scan, "--reset"], timeout=900)


def select_groups(ids, filters):
    ensure_groups()
    data = json.load(open(GJ, encoding="utf-8"))
    groups = data["groups"]
    if ids:
        return [g for g in groups if g["id"] in ids]
    if filters:
        return [g for g in groups if any(norm(f) in norm(g["name"]) for f in filters)]
    return []


def clear_draft():
    call("cdpkey", {"key": "Escape", "tabId": TAB})
    call("cdp", {"method": "Page.handleJavaScriptDialog", "params": {"accept": True}, "tabId": TAB})
    call("cdp", {"method": "Runtime.evaluate", "params": {
        "expression": "(()=>{const s=document.querySelectorAll('[contenteditable=true]');"
                      "for(const e of s){if(e.getClientRects().length){e.focus();document.execCommand('selectAll');document.execCommand('delete');}}"
                      "return s.length;})()",
        "returnByValue": True}, "tabId": TAB})


def bring(text, minw, lo, hi, want_y, exact=False, tries=8):
    it = wait_for(text, tries=tries, interval=0.4, minw=minw, lo=lo, hi=hi, exact=exact)
    if not it:
        return None
    dy = it["y"] - want_y
    if abs(dy) > 40:
        call("scroll", {"x": 0, "y": dy, "tabId": TAB})
        time.sleep(0.5)
        it2 = wait_for(text, tries=tries, interval=0.4, minw=minw, lo=lo, hi=hi, exact=exact)
        if it2:
            it = it2
    return it


def find_exact(text, minw=120, lo=20, hi=95):
    d = call("deepfind", {"text": text, "selector": "*", "visibleOnly": True, "limit": 800, "tabId": TAB})
    its = [i for i in d.get("items", []) if i.get("text", "").strip().lower() == text
           and i.get("w", 0) >= minw and lo <= i.get("h", 0) <= hi]
    if not its:
        return None
    btns = [i for i in its if (i.get("role") or "") == "button"]
    if btns:
        its = btns
    its.sort(key=lambda i: i["w"] * i["h"])
    return its[0]


def bring_exact(text, minw, lo, hi, want_y=300, tries=40):
    it = None
    for _ in range(tries):
        it = find_exact(text, minw, lo, hi)
        if it:
            break
        time.sleep(0.5)
    if not it:
        return None
    dy = it["y"] - want_y
    if abs(dy) > 40:
        call("scroll", {"x": 0, "y": dy, "tabId": TAB})
        time.sleep(0.5)
        it2 = find_exact(text, minw, lo, hi)
        if it2:
            it = it2
    return it


COMP_JS = r'''(()=>{function all(root,out){for(const e of root.querySelectorAll('*')){out.push(e);if(e.shadowRoot)all(e.shadowRoot,out);}}const out=[];all(document,out);let best=null,bd=1e9;for(const e of out){const t=((e.innerText||e.textContent||'')+'').toLowerCase();if(t.indexOf('escribe algo')<0||t.length>80)continue;const r=e.getBoundingClientRect();if(r.width>=150&&r.height>=18&&r.height<=140){const a=r.width*r.height;if(a<bd){bd=a;best=e;}}}if(!best)return 'none';best.scrollIntoView({block:'center'});const r=best.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()'''


def composer_rect(tries=20):
    for _ in range(tries):
        v = call("cdp", {"method": "Runtime.evaluate", "params": {"expression": COMP_JS, "returnByValue": True}, "tabId": TAB})
        v = v.get("result", {}).get("result", {}).get("value")
        if isinstance(v, dict):
            return v
        time.sleep(1)
    return None


def post_to_group(g, text, dry=False):
    call("tab.activate", {"tabId": TAB})
    time.sleep(0.6)
    clear_draft()
    call("navigate", {"tabId": TAB, "url": g["href"], "timeout": 45000})
    time.sleep(4.0)
    call("scroll", {"x": 0, "y": -200000, "tabId": TAB})
    time.sleep(0.8)
    comp = composer_rect()
    if not comp:
        return {"name": g["name"], "ok": False, "stage": "composer"}
    pub = None
    for _try in range(3):
        call("cdpclick", {"x": int(comp["x"] + comp["w"] / 2), "y": int(comp["y"] + comp["h"] / 2), "tabId": TAB})
        pub = bring_exact("publicar", 200, 30, 95, tries=8)
        if not pub:
            pub = bring_exact("publicar", 120, 25, 120, tries=4)
        if pub:
            break
        comp = composer_rect(tries=6) or comp
    if not pub:
        call("cdpkey", {"key": "Escape", "tabId": TAB})
        return {"name": g["name"], "ok": False, "stage": "dialog"}
    if not call("type", {"value": text, "tabId": TAB}).get("typed"):
        call("cdptype", {"text": text, "tabId": TAB})
    time.sleep(0.6)
    pub = bring_exact("publicar", 200, 30, 95, tries=8) or pub
    if dry:
        call("cdpkey", {"key": "Escape", "tabId": TAB})
        time.sleep(0.5)
        return {"name": g["name"], "ok": True, "dry": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
    for _attempt in range(2):
        pub = bring_exact("publicar", 200, 30, 95, tries=8) or pub
        call("cdpclick", {"x": pub["x"] + pub["w"] // 2, "y": pub["y"] + pub["h"] // 2, "tabId": TAB})
        for _ in range(30):
            time.sleep(0.7)
            if not find_exact("publicar", 200, 30, 95):
                return {"name": g["name"], "ok": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
    return {"name": g["name"], "ok": False, "stage": "verify", "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}


def write_status(st):
    tmp = STATUS + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, STATUS)


def job(cmd):
    filters = cmd.get("filter") or []
    if isinstance(filters, str):
        filters = [x for x in filters.split(",") if x.strip()]
    sel = select_groups(cmd.get("ids") or [], filters)
    limit = int(cmd.get("limit") or 0)
    if limit:
        sel = sel[:limit]
    dry = bool(cmd.get("dry"))
    interval = float(cmd.get("interval_sec") or 8)
    text = (cmd.get("text") or "").strip()
    st = {"running": True, "dry": dry, "total": len(sel), "done": 0, "published": 0,
          "started_at": time.time(), "finished_at": 0, "current": None, "results": []}
    write_status(st)
    for g in sel:
        st["current"] = g["name"]
        write_status(st)
        try:
            r = post_to_group(g, text, dry=dry)
        except Exception as e:
            r = {"name": g["name"], "ok": False, "stage": "exception", "error": str(e)[:120]}
        st["results"].append(r)
        st["done"] += 1
        if r.get("ok"):
            st["published"] += 1
        write_status(st)
        time.sleep(interval)
    st["running"] = False
    st["current"] = None
    st["finished_at"] = time.time()
    write_status(st)
    try:
        os.remove(PID)
    except OSError:
        pass


def main():
    if "--cmd" in sys.argv:
        p = sys.argv.index("--cmd")
        job(json.load(open(sys.argv[p + 1], encoding="utf-8")))
        return
    data = json.load(open(GJ, encoding="utf-8"))
    if "--list" in sys.argv:
        flt = []
        if "--filter" in sys.argv:
            flt = [x for x in sys.argv[sys.argv.index("--filter") + 1].split(",") if x.strip()]
        sel = select_groups([], flt)
        print(json.dumps({"count": len(sel), "names": [g["name"] for g in sel]}, ensure_ascii=False))
        return
    print(json.dumps({"total": data.get("count"), "categories": {k: len(v) for k, v in data.get("categories", {}).items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
