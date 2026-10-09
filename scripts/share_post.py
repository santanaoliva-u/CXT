#!/usr/bin/env python3
"""share_post.py - Replica del flujo de "ShareUnlimited" (compartir un post
YA EXISTENTE de Facebook a varios grupos) usando CXT (CDP).

Ported/reimplementado a partir de la ingenieria inversa de la extension
  mjdcihdmcknblacgdccgaeihdodfmffe (ShareUnlimited v19.3)
Selectores idioma-independientes, tal como los usa la extension:
  - Share dialog  : div[role=dialog][aria-labelledby] | [aria-label="Share to a group"]
  - picker listo  : input[type=search] + div[role=listitem]
  - search input  : input[type=search]
  - group row     : div[role=listitem] > div[role=button] + a[href*="/groups/"]
  - submit button : ultimo div[role=button] visible con 1<=texto<40 y aria-disabled!=true
  - audience chip : [aria-label^="Edit privacy. Sharing with"]

Uso:
  python3 share_post.py --ids "Nombre grupo 1,Nombre grupo 2" [--text "..."] [--delay 10] [--dry]
  python3 share_post.py --ids 123456789,987654321 --delay 0 --dry
  # --url <permalink> navega primero al post; si no, usa el post visible.
  # --dry (por defecto True): hace todo MENOS pulsar Publicar.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = os.environ.get("CXT_BIN") or os.path.join(HERE, "bin", "cxt")
TAB = int(os.environ["CXT_TAB"]) if os.environ.get("CXT_TAB") else None
HOME_DIR = os.path.expanduser("~")
STATUS = os.path.join(HOME_DIR, ".cxt", "share_post_status.json")


def call(op, args, timeout=120):
    p = subprocess.run([CXT, op, json.dumps(args)], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip())
    except Exception:
        return {"raw": (p.stdout or "")[:300], "err": (p.stderr or "")[:200]}


def cdp(expr, timeout=90):
    r = call("cdp", {"tabId": TAB, "method": "Runtime.evaluate",
                     "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}, timeout)
    try:
        return r["result"]["result"].get("value")
    except Exception:
        return None


def click(x, y):
    return call("cdpclick", {"tabId": TAB, "x": int(x), "y": int(y)})


def sleep(ms):
    time.sleep(ms / 1000.0)


# ---------------------------------------------------------------- JS helpers
JS_VISIBLE = "const vis=(e)=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>4&&r.height>4&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';};"

JS_FIND_SHARE = "(()=>{" + JS_VISIBLE + """
 const want=['share','compartir'];
 for(const el of document.querySelectorAll('[aria-label]')){
   const a=(el.getAttribute('aria-label')||'').trim().toLowerCase();
   if(!want.includes(a)) continue;
   if(!vis(el)) continue; const r=el.getBoundingClientRect();
   if(r.width>18 && r.width<92 && r.height>14 && r.y>30 && r.y<620)
     return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};
 } return null;})()"""

JS_GROUP_OPTION = "(()=>{" + JS_VISIBLE + """
 const want=['share to a group','compartir en un grupo'];
 for(const el of document.querySelectorAll('[aria-label]')){
   const a=(el.getAttribute('aria-label')||'').trim().toLowerCase();
   if(want.includes(a)&&vis(el)){const r=el.getBoundingClientRect();
     if(r.width>30) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+Math.min(18,r.height/2))};}
 }
 let best=null;
 for(const b of document.querySelectorAll('div[role=button]')){
   const al=(b.getAttribute('aria-label')||'').toLowerCase();
   const t=(b.textContent||'').trim().toLowerCase();
   if(!vis(b)) continue;
   if(/grupo/.test(al) || /^grupo$/.test(t)){const r=b.getBoundingClientRect();
     if(r.width>40&&r.y>40&&r.y<640) best={x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
 }
 return best;})()"""

JS_SCROLL_MODAL = "(()=>{const el=[...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.x>200&&r.x<745&&r.y>40&&r.y<645&&r.height>250&&(s.overflowY==='auto'||s.overflowY==='scroll');})[0];if(el){el.scrollBy(0,360);return true;}return false;})()"""

JS_PICKER = "(()=>{" + JS_VISIBLE + """
 const inp=[...document.querySelectorAll('input[type=search]')].filter(vis).find(i=>{
   const a=((i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();
   const r=i.getBoundingClientRect();
   return r.x>200 && r.y>40 && /grupo|group/.test(a);});
 if(inp){const r=inp.getBoundingClientRect();
   return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
 return null;})()"""

JS_RESET = "(()=>{" + JS_VISIBLE + """
 const b=[...document.querySelectorAll('div[role=button],button,a[role=button]')].find(e=>{
   const t=(e.textContent||'').trim().toLowerCase();
   return (t==='reset'||t==='restablecer'||t==='limpiar'||t==='clear')&&vis(e);});
 if(b){const r=b.getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
 return null;})()"""

JS_CLEAR_ACTIVE = """(()=>{const el=document.activeElement;
 if(!el) return false;
 if(el.tagName==='INPUT'||el.tagName==='TEXTAREA'){
   const s=Object.getOwnPropertyDescriptor(el.constructor.prototype,'value').set;
   s.call(el,''); el.dispatchEvent(new Event('input',{bubbles:true})); return true;}
 if(el.isContentEditable){el.innerHTML='';el.dispatchEvent(new InputEvent('input',{bubbles:true}));return true;}
 return false;})()"""

JS_AUDIENCE = "(()=>{" + JS_VISIBLE + """
 const chips=[...document.querySelectorAll('div[role=dialog] [aria-label^="Edit privacy. Sharing with"]')].filter(vis)
   .map(c=>(c.getAttribute('aria-label')||''));
 return {chips:chips.slice(0,4), group: chips.some(a=>/group|grupo/i.test(a))};})()"""

JS_SUBMIT = "(()=>{" + JS_VISIBLE + """
 const btns=[...document.querySelectorAll('div[role=button],button,a[role=button]')].filter(vis).filter(b=>{
   const t=(b.textContent||'').trim(); return t.length>2 && t.length<45;});
 const pick=(re)=>{for(let i=btns.length-1;i>=0;i--){const t=(btns[i].textContent||'').trim();
   if(re.test(t)) return btns[i];} return null;};
 const b=pick(/share to selected/i)||pick(/^(publicar|compartir ahora|compartir)$/i)||pick(/publicar|compartir/i);
 if(!b) return null;
 if((b.getAttribute('aria-disabled')||'')==='true') return {disabled:true};
 const r=b.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+Math.round(r.height*0.4)),label:(b.textContent||'').trim()};})()"""


def js_select_row(name):
    n = json.dumps(name)
    return "(()=>{" + JS_VISIBLE + """
 const norm=(s)=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
 const tgt=norm(""" + n + """);
 const hit=(t)=>t.includes(tgt)||(tgt.length>6&&tgt.includes(t));
 for(const b of document.querySelectorAll('div[role=button]')){
   if(!vis(b)) continue; const cb=b.querySelector('input[type=checkbox]');
   if(cb && hit(norm(b.textContent||''))){const r=cb.getBoundingClientRect();
     if(r.width>2) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),t:(b.textContent||'').trim().slice(0,50)};}
 }
 for(const cb of document.querySelectorAll('input[type=checkbox]')){
   if(!vis(cb)) continue; let e=cb;
   for(let i=0;i<7&&e;i++){e=e.parentElement; if(e&&hit(norm(e.textContent||''))){const r=cb.getBoundingClientRect();
     return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),t:(e.textContent||'').trim().slice(0,50)};}}
 }
 for(const b of document.querySelectorAll('div[role=button],a[role=button]')){
   if(!vis(b)) continue; const t=norm(b.textContent||'');
   if(hit(t)){const r=b.getBoundingClientRect();
     if(r.width>60&&r.y>60&&r.y<640) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),t:(b.textContent||'').trim().slice(0,50)};}
 }
 return null;})()"""


# ---------------------------------------------------------------- actions
def ensure_tab():
    global TAB
    if TAB is None:
        r = call("tabs", {})
        for t in (r if isinstance(r, list) else []):
            if "facebook.com" in (t.get("url") or ""):
                TAB = t["id"]
                break
    if TAB is None:
        sys.exit("no encontre pestana de Facebook (set CXT_TAB)")
    call("tab.activate", {"tabId": TAB})


JS_BRING_SHARE = """(()=>{const el=document.querySelector('[aria-label="Share"],[aria-label="Compartir"]');
 if(el){el.scrollIntoView({block:'center'});return true;}return false;})()"""


def open_share():
    for _ in range(18):
        if cdp(JS_BRING_SHARE):
            sleep(600)
            r = cdp(JS_FIND_SHARE)
            if r:
                click(r["x"], r["y"])
                sleep(1500)
                return True
        else:
            cdp("(()=>{const s=document.scrollingElement||document.documentElement;"
                "s.scrollBy(0,480);window.scrollBy(0,480);return true;})()")
        sleep(800)
    return False


def open_group_option():
    for _ in range(14):
        if cdp(JS_PICKER):
            r = cdp(JS_RESET)          # la extension Multi-Share persiste selecciones: limpiar
            if r:
                click(r["x"], r["y"])
                sleep(900)
                r2 = cdp(JS_RESET)     # por si hay un cuadro de confirmacion
                if r2:
                    click(r2["x"], r2["y"])
                    sleep(600)
            return True
        g = cdp(JS_GROUP_OPTION)
        if g:
            click(g["x"], g["y"])
            sleep(1600)
            if cdp(JS_PICKER):
                r = cdp(JS_RESET)
                if r:
                    click(r["x"], r["y"])
                    sleep(900)
                return True
        else:
            cdp(JS_SCROLL_MODAL)
        sleep(700)
    return bool(cdp(JS_PICKER))


def search_and_select(name):
    p = cdp(JS_PICKER)
    if not p:
        return False
    click(p["x"], p["y"])           # focus search input
    sleep(300)
    cdp(JS_CLEAR_ACTIVE)            # limpia (native setter + input event)
    call("cdptype", {"tabId": TAB, "text": name})
    time.sleep(1.6)
    for _ in range(8):
        row = cdp(js_select_row(name))
        if row:
            click(row["x"], row["y"])
            sleep(1500)
            return True
        time.sleep(0.8)
    return False


def submit(dry, text=""):
    aud = cdp(JS_AUDIENCE)
    if aud and aud.get("chips") and not aud.get("group"):
        print("   ABORTO: la audiencia no es un grupo ->", aud.get("chips"))
        return {"ok": False, "stage": "audience"}
    if text:
        call("cdptype", {"tabId": TAB, "text": text})
        sleep(400)
    s = cdp(JS_SUBMIT)
    if not s:
        return {"ok": False, "stage": "submit-not-found"}
    if s.get("disabled"):
        return {"ok": False, "stage": "submit-disabled"}
    if dry:
        return {"ok": True, "dry": True, "label": s.get("label"), "x": s["x"], "y": s["y"]}
    click(s["x"], s["y"])
    time.sleep(2)
    return {"ok": True, "published": True, "label": s.get("label")}


def close_dialog():
    for _ in range(3):
        call("cdpkey", {"tabId": TAB, "key": "Escape"})
        sleep(600)
        if not cdp(JS_SUBMIT):
            return True
    return False


def delay_seconds(base):
    return max(0, float(base))


def _norm(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")


def load_groups():
    p = os.path.join(HOME_DIR, ".cxt", "groups.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("groups", [])
    except Exception:
        return []


def select_groups(ids, filt, limit):
    if ids:
        return [g.strip() for g in ids.split(",") if g.strip()]
    kws = [_norm(x) for x in (filt or "").split(",") if x.strip()]
    if not kws:
        return []
    out = []
    for g in load_groups():
        n = _norm(g.get("name", ""))
        if all(k in n for k in kws):
            out.append(g.get("name") or g.get("id"))
    if limit and limit > 0:
        out = out[:limit]
    return out


def write_status(d):
    try:
        tmp = STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATUS)
    except Exception:
        pass


def run_from_cmd(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    url = (cfg.get("url") or "").strip()
    text = cfg.get("text") or ""
    dry = bool(cfg.get("dry", True))
    delay = max(0.0, float(cfg.get("delay", 8) or 0))
    limit = int(cfg.get("limit", 0) or 0)
    groups = select_groups(cfg.get("ids") or "", cfg.get("filter") or "", limit)
    st = {"running": True, "dry": dry, "total": len(groups), "done": 0, "published": 0,
          "current": "", "results": [], "started_at": int(time.time() * 1000), "finished_at": 0}
    write_status(st)
    if not groups:
        st.update(running=False, error="sin grupos (revisa filter/ids)", finished_at=int(time.time() * 1000))
        write_status(st)
        return

    import signal
    def on_term(*_):
        st.update(running=False, current="", error="detenido", finished_at=int(time.time() * 1000))
        write_status(st)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    ensure_tab()
    if url:
        call("navigate", {"tabId": TAB, "url": url, "timeout": 45000})
        sleep(4000)
    for i, g in enumerate(groups):
        st["current"] = g
        st["done"] = i
        write_status(st)
        step = {"group": g}
        if not open_share():
            step.update(ok=False, stage="share-button")
        elif not open_group_option():
            step.update(ok=False, stage="group-option")
            close_dialog()
        elif not search_and_select(g):
            step.update(ok=False, stage="select")
            close_dialog()
        else:
            step.update(submit(dry, text))
            if step.get("ok") and not dry:
                st["published"] = st.get("published", 0) + 1
        st["results"].append(step)
        st["done"] = i + 1
        write_status(st)
        if i < len(groups) - 1:
            close_dialog()
            sleep(int(delay * 1000))
    st.update(running=False, current="", finished_at=int(time.time() * 1000))
    write_status(st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--delay", type=float, default=10)
    ap.add_argument("--dry", action="store_true", default=True)
    ap.add_argument("--no-dry", dest="dry", action="store_false")
    a = ap.parse_args()
    if a.cmd:
        run_from_cmd(a.cmd)
        return
    if not a.ids:
        sys.exit("usa --ids o --cmd")
    ensure_tab()
    if a.url:
        call("navigate", {"tabId": TAB, "url": a.url, "timeout": 45000})
        sleep(4000)
    groups = [g.strip() for g in a.ids.split(",") if g.strip()]
    results = []
    for i, g in enumerate(groups):
        print(f"[{i+1}/{len(groups)}] {g}")
        step = {"group": g}
        if not open_share():
            step.update(ok=False, stage="share-button")
            results.append(step)
            continue
        if not open_group_option():
            step.update(ok=False, stage="group-option")
            close_dialog()
            results.append(step)
            continue
        if not search_and_select(g):
            step.update(ok=False, stage="select")
            close_dialog()
            results.append(step)
            continue
        r = submit(a.dry, a.text)
        step.update(r)
        results.append(step)
        if i < len(groups) - 1:
            close_dialog()
            sleep(int(delay_seconds(a.delay) * 1000))
    print(json.dumps({"dry": a.dry, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
