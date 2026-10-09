#!/usr/bin/env python3
"""comment.py - Publica comentarios en Facebook con CXT (CDP).

- Modo directo : python3 comment.py --url <post|feed> --text "..." [--dry]
- Modo job     : python3 comment.py --cmd <json>   (lo usa el daemon /comment)
  El job itera <count> veces cada <interval_min> minutos; si el target es un
  feed (pagina/grupo/perfil) comenta la publicacion mas nueva que aun no haya
  comentado (dedup en ~/.cxt/commented.json).

Selectores verificados (2026-10-09):
  - caja de comentario : div[role=textbox][contenteditable=true] con aria-label
                         /comentar como|write a comment|escribe un comentario/
  - submit             : no hay boton visible -> se envia con Enter.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = os.environ.get("CXT_BIN") or os.path.join(HERE, "bin", "cxt")
TAB = int(os.environ["CXT_TAB"]) if os.environ.get("CXT_TAB") else None
HOME_DIR = os.path.expanduser("~")
STATUS = os.path.join(HOME_DIR, ".cxt", "comment_status.json")
COMMENTED = os.path.join(HOME_DIR, ".cxt", "commented.json")
NEWS = os.path.join(HERE, "news.sh")


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


JS_VISIBLE = "const vis=(e)=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>4&&r.height>4&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';};"

JS_FIND_TARGET = "(()=>{" + JS_VISIBLE + """
 const boxes=[...document.querySelectorAll('div[contenteditable=true]')].filter(e=>{
   const a=(e.getAttribute('aria-label')||'').toLowerCase();
   return vis(e) && (e.getAttribute('role')==='textbox' || /comentar como|write a comment|comment as|escribe un comentario/.test(a));});
 if(!boxes.length) return {n:0};
 boxes.sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y);
 const box=boxes[0];
 box.scrollIntoView({block:'center'});
 const r=box.getBoundingClientRect();
 return {n:boxes.length, box:{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),a:(box.getAttribute('aria-label')||'')}};})()"""

def js_find_target_by_text(txt):
    n = json.dumps((txt or "")[:60])
    return "(()=>{" + JS_VISIBLE + """
 const want=""" + n + """.toLowerCase();
 const boxes=[...document.querySelectorAll('div[contenteditable=true]')].filter(e=>{
   const a=(e.getAttribute('aria-label')||'').toLowerCase();
   return vis(e) && (e.getAttribute('role')==='textbox' || /comentar como|write a comment|comment as/.test(a));});
 let pick=null;
 if(want) pick=boxes.find(b=>{let e=b,k=0;while(e&&k<25){if((e.innerText||'').toLowerCase().includes(want))return true;e=e.parentElement;k++;}return false;});
 if(!pick) pick=boxes.sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y)[0];
 if(!pick) return null;
 pick.scrollIntoView({block:'center'});
 const r=pick.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),a:(pick.getAttribute('aria-label')||'')};})()"""

JS_FOCUS_TARGET = "(()=>{" + JS_VISIBLE + """
 const boxes=[...document.querySelectorAll('div[contenteditable=true]')].filter(e=>{
   const a=(e.getAttribute('aria-label')||'').toLowerCase();
   return vis(e) && (e.getAttribute('role')==='textbox' || /comentar como|write a comment|comment as|escribe un comentario/.test(a));});
 if(!boxes.length) return {n:0};
 boxes.sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y);
 const box=boxes[0];
 box.scrollIntoView({block:'center'});
 box.focus();
 const r=box.getBoundingClientRect();
 return {n:boxes.length, focused:!!(document.activeElement&&(document.activeElement===box||box.contains(document.activeElement))), box:{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),a:(box.getAttribute('aria-label')||'')}};})()"""

def js_focus_by_text(txt):
    n = json.dumps((txt or "")[:60])
    return "(()=>{" + JS_VISIBLE + """
 const want=""" + n + """.toLowerCase();
 const boxes=[...document.querySelectorAll('div[contenteditable=true]')].filter(e=>{
   const a=(e.getAttribute('aria-label')||'').toLowerCase();
   return vis(e) && (e.getAttribute('role')==='textbox' || /comentar como|write a comment|comment as/.test(a));});
 let pick=null;
 if(want) pick=boxes.find(b=>{let e=b,k=0;while(e&&k<25){if((e.innerText||'').toLowerCase().includes(want))return true;e=e.parentElement;k++;}return false;});
 if(!pick) pick=boxes.sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y)[0];
 if(!pick) return {n:0};
 pick.scrollIntoView({block:'center'});
 pick.focus();
 const r=pick.getBoundingClientRect();
 return {n:boxes.length, focused:!!(document.activeElement&&(document.activeElement===pick||pick.contains(document.activeElement))), box:{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),a:(pick.getAttribute('aria-label')||'')}};})()"""

JS_CLEAR = """(()=>{const el=document.activeElement; if(!el) return false;
  if(el.isContentEditable){el.innerHTML='';el.dispatchEvent(new InputEvent('input',{bubbles:true}));return true;}
  if(el.tagName==='INPUT'||el.tagName==='TEXTAREA'){const s=Object.getOwnPropertyDescriptor(el.constructor.prototype,'value').set;
    s.call(el,'');el.dispatchEvent(new Event('input',{bubbles:true}));return true;} return false;})()"""

JS_IS_FOCUSED = "(()=>{const e=document.activeElement;return !!(e&&(e.isContentEditable||((e.getAttribute&&e.getAttribute('role'))==='textbox')));})()"

JS_ACTION_COMMENT = "(()=>{" + JS_VISIBLE + """
 const b=[...document.querySelectorAll('[aria-label]')].find(e=>{
   const a=(e.getAttribute('aria-label')||'').toLowerCase();
   return /dejar un comentario|leave a comment|^comentar$|^comment$/.test(a)&&vis(e);});
 if(b){b.scrollIntoView({block:'center'});const r=b.getBoundingClientRect();
   return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
 return null;})()"""

JS_BOX_TEXT = "(()=>{const e=document.activeElement; if(!e) return ''; if(e.isContentEditable) return (e.innerText||'').trim().slice(0,80); if(e.tagName==='INPUT'||e.tagName==='TEXTAREA') return (e.value||'').trim().slice(0,80); return '';})()"

# boton de enviar comentario (por si aparece); si no, se usa Enter.
JS_SUBMIT_COMMENT = "(()=>{" + JS_VISIBLE + """
 const cands=[...document.querySelectorAll('div[role=button],button,[aria-label]')].filter(vis).filter(b=>{
   const a=(b.getAttribute('aria-label')||'').toLowerCase();
   return /comentar$|publicar comentario|post comment|enviar comentario|^comment$/.test(a);});
 for(let i=cands.length-1;i>=0;i--){const r=cands[i].getBoundingClientRect();
   if(r.height>10&&r.height<60) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),a:(cands[i].getAttribute('aria-label')||'')};}
 return null;})()"""


def js_menu_btn(txt):
    n = json.dumps((txt or "")[:60])
    return "(()=>{" + JS_VISIBLE + """
 const T=""" + n + """;
 let host=null;
 for(const e of document.querySelectorAll('div[role=article],div,span')){const t=(e.innerText||'');if(t.includes(T)&&t.length<300&&vis(e)){host=e;break;}}
 if(!host) return 'NOTFOUND';
 host.scrollIntoView({block:'center'});
 const b=host.querySelector('[aria-haspopup="menu"]')||host.querySelector('[aria-label*="Editar o eliminar"]');
 if(!b) return 'NOMENU';
 const r=b.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};})()"""

JS_MENU_ITEM = "(()=>{" + JS_VISIBLE + """
 let best=null;
 for(const e of document.querySelectorAll('[role="menuitem"],div[role="button"],span,div')){
   const t=(e.innerText||'').trim();
   if(t!=='Eliminar' && t!=='Delete') continue;
   if(!vis(e)) continue;
   const r=e.getBoundingClientRect();
   if(r.width<20||r.height<10) continue;
   if(!best||r.width<best.w) best={x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),w:r.width};
 }
 return best?{x:best.x,y:best.y}:null;})()"""

JS_CONFIRM_DEL = "(()=>{" + JS_VISIBLE + """
 const cands=[...document.querySelectorAll('[role="dialog"] div[role="button"],[role="dialog"] span,[role="button"]')].filter(e=>{
   const t=(e.innerText||'').trim(); return (/^(Eliminar|Delete)$/i.test(t)) && vis(e);});
 cands.sort((a,b)=>b.getBoundingClientRect().y-a.getBoundingClientRect().y);
 const e=cands[0]; if(!e) return null;
 const r=e.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};})()"""


def delete_comment(text):
    mb = None
    for _ in range(10):
        r = cdp(js_menu_btn(text))
        if isinstance(r, dict) and r.get("x") is not None:
            mb = r
            break
        sleep(600)
    if not mb:
        return {"ok": False, "stage": "no-menu-btn"}
    click(mb["x"], mb["y"])
    sleep(1000)
    mi = None
    for _ in range(8):
        r = cdp(JS_MENU_ITEM)
        if isinstance(r, dict) and r.get("x") is not None:
            mi = r
            break
        sleep(500)
    if not mi:
        return {"ok": False, "stage": "no-menu-item"}
    click(mi["x"], mi["y"])
    sleep(1200)
    cf = cdp(JS_CONFIRM_DEL)
    if isinstance(cf, dict) and cf.get("x") is not None:
        click(cf["x"], cf["y"])
        sleep(1500)
    return {"ok": True, "deleted": text[:40]}


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


def _norm(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")


def gen_auto():
    try:
        out = subprocess.run([NEWS, "Playa del Carmen", "6"], capture_output=True, text=True, timeout=60).stdout
        for line in out.splitlines():
            t = line.strip().split(" - ")[0].strip()
            if t:
                return random.choice([
                    "Muy de acuerdo con esto de Playa del Carmen 🌴",
                    "Buen dato para Playa del Carmen, gracias por compartir. 👏",
                    "Esto nos toca a todos en Playa del Carmen. 🌴",
                ])
    except Exception:
        pass
    return "Excelente publicación. 🌴"


def is_permalink(url):
    return bool(re.search(r"/posts/|/videos/|story_fbid|/photo|permalink|fbid=", url or ""))


def load_commented():
    try:
        with open(COMMENTED, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_commented(lst):
    try:
        tmp = COMMENTED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lst[-500:], f, ensure_ascii=False)
        os.replace(tmp, COMMENTED)
    except Exception:
        pass


def write_status(d):
    try:
        tmp = STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATUS)
    except Exception:
        pass


def comment_once(text, url, only_text=None, dry=False):
    if url:
        call("navigate", {"tabId": TAB, "url": url, "timeout": 45000})
        sleep(4200)
    focused = False
    tgt = None
    for _ in range(10):
        r = cdp(js_focus_by_text(only_text)) if only_text else cdp(JS_FOCUS_TARGET)
        if r and r.get("n"):
            tgt = r
            if r.get("focused") or cdp(JS_IS_FOCUSED):
                focused = True
                break
            if r.get("box"):
                click(r["box"]["x"], r["box"]["y"])
                sleep(500)
                if cdp(JS_IS_FOCUSED):
                    focused = True
                    break
        sleep(600)
    if not focused:
        act = cdp(JS_ACTION_COMMENT)
        for _ in range(3):
            if act:
                click(act["x"], act["y"])
            sleep(700)
            r = cdp(js_focus_by_text(only_text)) if only_text else cdp(JS_FOCUS_TARGET)
            if r and (r.get("focused") or cdp(JS_IS_FOCUSED)):
                tgt = r
                focused = True
                break
            act = cdp(JS_ACTION_COMMENT)
    if not focused:
        return {"ok": False, "stage": "no-focus"}
    cdp(JS_CLEAR)
    call("cdptype", {"tabId": TAB, "text": text})
    sleep(900)
    got = cdp(JS_BOX_TEXT)
    if not got:
        return {"ok": False, "stage": "type-failed"}
    if dry:
        cdp(JS_CLEAR)
        call("cdpkey", {"tabId": TAB, "key": "Escape"})
        return {"ok": True, "dry": True, "typed": got, "key": ((tgt or {}).get("key") or "")[:80]}
    sub = cdp(JS_SUBMIT_COMMENT)
    if sub:
        click(sub["x"], sub["y"])
    else:
        call("cdpkey", {"tabId": TAB, "key": "Enter"})
    sleep(2200)
    left = cdp(JS_BOX_TEXT)
    ok = (left == "") or (text[:20] not in (left or ""))
    return {"ok": bool(ok), "posted": bool(ok), "key": ((tgt or {}).get("key") or "")[:80], "submit": ("button" if sub else "enter")}


def run_from_cmd(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    url = (cfg.get("url") or "").strip()
    text = (cfg.get("text") or "").strip()
    auto = bool(cfg.get("auto", not text))
    count = max(1, int(cfg.get("count", 1) or 1))
    interval = max(0.0, float(cfg.get("interval_min", 0) or 0))
    dry = bool(cfg.get("dry", True))
    st = {"running": True, "dry": dry, "total": count, "done": 0, "posted": 0,
          "current": "", "results": [], "started_at": int(time.time() * 1000), "finished_at": 0}
    write_status(st)

    import signal
    def on_term(*_):
        st.update(running=False, current="", error="detenido", finished_at=int(time.time() * 1000))
        write_status(st)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    ensure_tab()
    commented = load_commented()
    for i in range(count):
        msg = gen_auto() if auto else text
        st["current"] = msg[:60]
        st["done"] = i
        write_status(st)
        step = {"i": i + 1, "text": msg[:80]}
        step.update(comment_once(msg, url, dry=dry))
        if step.get("ok") and not dry:
            st["posted"] = st.get("posted", 0) + 1
            commented.append(step.get("key") or msg[:60])
            save_commented(commented)
        st["results"].append(step)
        st["done"] = i + 1
        write_status(st)
        if i < count - 1 and interval > 0:
            time.sleep(interval * 60)
    st.update(running=False, current="", finished_at=int(time.time() * 1000))
    write_status(st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--delete", default="")
    ap.add_argument("--dry", action="store_true", default=True)
    ap.add_argument("--no-dry", dest="dry", action="store_false")
    a = ap.parse_args()
    if a.cmd:
        run_from_cmd(a.cmd)
        return
    if a.delete:
        ensure_tab()
        print(json.dumps(delete_comment(a.delete), ensure_ascii=False))
        return
    if not a.text:
        sys.exit("usa --text o --cmd")
    ensure_tab()
    print(json.dumps(comment_once(a.text, a.url, dry=a.dry), ensure_ascii=False))


if __name__ == "__main__":
    main()
