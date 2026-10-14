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
import sys
import time

import cxtlib

CXT = cxtlib.resolve_cxt()
TAB = cxtlib.resolve_tab(CXT)
STATUS = os.path.join(cxtlib.resolve_dir(), "share_post_status.json")
call = cxtlib.caller(CXT)
EXPECT = ""  # texto que identifica el post a compartir (para el fallback sin ventana)


def sleep(ms):
    time.sleep(ms / 1000.0)


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def wait_js(expr, tries=12, interval=0.3):
    for _ in range(max(1, tries)):
        r = cxtlib.cdp(CXT, TAB, expr)
        if r:
            return r
        sleep(int(interval * 1000))
    return None


# ---------------------------------------------------------------- JS helpers
JS_VISIBLE = "const vis=(e)=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>4&&r.height>4&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';};"

def js_find_share(expect=""):
    e = json.dumps(expect or "")
    return "(()=>{" + JS_VISIBLE + """
 const norm=(s)=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
 const w=norm(""" + e + """);
 const all=[...document.querySelectorAll('[aria-label]')].filter(el=>/^(share|compartir)$/i.test((el.getAttribute('aria-label')||'').trim())).filter(vis);
 if(!all.length) return null;
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).find(d=>d.querySelector('[aria-label="Share"],[aria-label="Compartir"],[role=article]'));
 let el=null, scope='none', box=null;
 if(dlg){ box=dlg; }
 else if(w){ box=[...document.querySelectorAll('[role=article]')].find(a=>norm(a.innerText||'').includes(w))||null; }
 else { box=[...document.querySelectorAll('[role=article]')].find(a=>a.querySelector('[aria-label="Share"],[aria-label="Compartir"]'))||null; }
 if(box){
   if(w && !norm(box.innerText||'').includes(w)) return null;
   el=all.find(x=>box.contains(x))||null;
   scope=dlg?'dialog':'article';
 }
  if(!el){
    // Sin expect (o basura ya filtrada): el post visible de la pestaña dedicada.
    if(!w){
      const main=document.querySelector('[role=main]')||document.body;
      const cands=all.filter(e=>main.contains(e)).sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y);
      el=cands[0]||null; scope='main';
    } else {
      // Permalink de foto/video sin role=article: el Share del main es el del post.
      if(/\\/posts\\/|\\/permalink|\\/share\\/p\\/|\\/photo|\\/videos\\//.test(location.href)){
        const main=document.querySelector('[role=main]')||document.body;
        const cands=all.filter(e=>main.contains(e)).sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y);
        el=cands[0]||null; scope='main';
      }
      if(!el) return null;
    }
  }
 if(!el) return null;
 el.scrollIntoView({block:'center'});
 const r=el.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),scope};})()"""


JS_POST_CTX = "(()=>{" + JS_VISIBLE + """
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).find(d=>d.querySelector('[aria-label="Share"],[aria-label="Compartir"],[role=article]'));
 const permalink=/\\/posts\\/|\\/permalink|\\/share\\/p\\/|\\/photo|\\/videos\\//.test(location.href);
 return {dialog:!!dlg, permalink, url:location.href};})()"""

JS_POST_TEXT = "(()=>{" + JS_VISIBLE + """
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).find(d=>d.querySelector('[aria-label="Share"],[aria-label="Compartir"],[role=article]'));
 const root=dlg||document.querySelector('[role=main]')||document.body;
 return (root.innerText||'').slice(0,4000);})()"""

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
   if(/grupo/.test(al) || /grupo/.test(t)){const r=b.getBoundingClientRect();
     if(r.width>40&&r.y>40&&r.y<640) best={x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
 }
 return best;})()"""

JS_MENU = "(()=>{" + JS_VISIBLE + """
 const norm=(s)=>(s||'').replace(/\\s+/g,' ').trim().toLowerCase();
 for(const el of document.querySelectorAll('[aria-label]')){
   const a=(el.getAttribute('aria-label')||'').trim().toLowerCase();
   if((a==='compartir en un grupo'||a==='share to a group')&&vis(el)) return {open:true};
 }
 for(const b of document.querySelectorAll('div[role=button],a[role=button]')){
   if(!vis(b)) continue; const t=norm(b.textContent||'');
   if(t==='compartir ahora'||t==='share now') return {open:true};
 }
 return null;})()"""

JS_SCROLL_MODAL = "(()=>{const el=[...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.x>200&&r.x<745&&r.y>40&&r.y<645&&r.height>250&&(s.overflowY==='auto'||s.overflowY==='scroll');})[0];if(el){el.scrollBy(0,360);return true;}return false;})()"""

JS_PICKER = "(()=>{" + JS_VISIBLE + """
 const inp=[...document.querySelectorAll('input[type=search]')].filter(vis).find(i=>{
   const a=((i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();
   const r=i.getBoundingClientRect();
   return r.x>200 && r.y>40 && /grupo|group/.test(a);});
 if(!inp) return null;
 inp.scrollIntoView({block:'center'});
 inp.focus();
 const r=inp.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),focused:document.activeElement===inp,value:inp.value};})()"""

JS_PICKER_VALUE = "(()=>{" + JS_VISIBLE + """
 const inp=[...document.querySelectorAll('input[type=search]')].filter(vis).find(i=>{const a=((i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();return /grupo|group/.test(a);});
 return inp?inp.value:null;})()"""

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
 const b=pick(/share to selected/i)||pick(/^(publicar|post)$/i)||pick(/publicar/i)||pick(/compartir/i);
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
def open_share():
    # NUNCA Escape aqui: cerraria la VENTANA (modal) del post y compartiriamos
    # el feed de fondo (post equivocado). Si el menu/picker ya esta abierto, listo.
    if cxtlib.cdp(CXT, TAB, JS_PICKER) or cxtlib.cdp(CXT, TAB, JS_MENU):
        return True
    for _ in range(3):
        f = None
        for _ in range(10):
            f = cxtlib.cdp(CXT, TAB, js_find_share(EXPECT))
            if f:
                break
            cxtlib.cdp(CXT, TAB, "(()=>{const s=document.scrollingElement||document.documentElement;"
                "s.scrollBy(0,420);window.scrollBy(0,420);return true;})()")
            sleep(250)
        if not f:
            continue
        cxtlib.click(CXT, TAB, f["x"], f["y"])
        if wait_js(JS_MENU, 16, 0.25):
            return True
        sleep(300)
    return False


def open_group_option():
    if cxtlib.cdp(CXT, TAB, JS_PICKER):
        return True
    for _ in range(10):
        g = cxtlib.cdp(CXT, TAB, JS_GROUP_OPTION)
        if g:
            cxtlib.click(CXT, TAB, g["x"], g["y"])
            if wait_js(JS_PICKER, 16, 0.25):
                r = cxtlib.cdp(CXT, TAB, JS_RESET)      # la extension Multi-Share persiste selecciones: limpiar
                if r:
                    cxtlib.click(CXT, TAB, r["x"], r["y"])
                    sleep(400)
                return True
        else:
            cxtlib.cdp(CXT, TAB, JS_SCROLL_MODAL)
        sleep(350)
    return bool(cxtlib.cdp(CXT, TAB, JS_PICKER))


def _type_search(name):
    cxtlib.cdp(CXT, TAB, JS_CLEAR_ACTIVE)            # limpia (native setter + input event)
    call("cdptype", {"tabId": TAB, "text": name})
    time.sleep(0.8)
    v = cxtlib.cdp(CXT, TAB, JS_PICKER_VALUE) or ""
    return len(v) > 0


def search_and_select(name):
    p = wait_js(JS_PICKER, 12, 0.25)  # JS_PICKER ya hace scrollIntoView + focus
    if not p:
        return False
    sleep(150)
    if not _type_search(name):
        wait_js(JS_PICKER, 4, 0.3)
        _type_search(name)
    for _ in range(24):
        row = cxtlib.cdp(CXT, TAB, js_select_row(name))
        if row:
            cxtlib.click(CXT, TAB, row["x"], row["y"])
            for _ in range(16):     # avanzar = aparece el boton Publicar del paso final
                sleep(250)
                if cxtlib.cdp(CXT, TAB, JS_SUBMIT):
                    return True
            return True
        time.sleep(0.35)
    return False


def submit(dry, text=""):
    aud = cxtlib.cdp(CXT, TAB, JS_AUDIENCE)
    if aud and aud.get("chips") and not aud.get("group"):
        log("   ABORTO: la audiencia no es un grupo -> %s" % aud.get("chips"))
        return {"ok": False, "stage": "audience"}
    if text:
        call("cdptype", {"tabId": TAB, "text": text})
        sleep(250)
    s = cxtlib.cdp(CXT, TAB, JS_SUBMIT)
    if not s:
        return {"ok": False, "stage": "submit-not-found"}
    if s.get("disabled"):
        return {"ok": False, "stage": "submit-disabled"}
    if dry:
        return {"ok": True, "dry": True, "label": s.get("label"), "x": s["x"], "y": s["y"]}
    cxtlib.click(CXT, TAB, s["x"], s["y"])
    time.sleep(2)
    return {"ok": True, "published": True, "label": s.get("label")}


def close_dialog():
    # cierra SOLO el menu/picker de compartir; NO la ventana (modal) del post.
    for _ in range(3):
        if not (cxtlib.cdp(CXT, TAB, JS_PICKER) or cxtlib.cdp(CXT, TAB, JS_MENU) or cxtlib.cdp(CXT, TAB, JS_SUBMIT)):
            return True
        call("cdpkey", {"tabId": TAB, "key": "Escape"})
        call("cdp", {"tabId": TAB, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}})
        sleep(600)
    return not (cxtlib.cdp(CXT, TAB, JS_PICKER) or cxtlib.cdp(CXT, TAB, JS_MENU) or cxtlib.cdp(CXT, TAB, JS_SUBMIT))


def goto(url):
    call("cdp", {"tabId": TAB, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}})
    cxtlib.clear_draft(CXT, TAB)
    call("navigate", {"tabId": TAB, "url": url, "timeout": 20000})
    call("cdp", {"tabId": TAB, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}})
    sleep(250)


def is_boilerplate_expect(expect):
    n = cxtlib.norm(expect or "")
    if not n:
        return True
    words = [w for w in n.split() if w]
    noise = {"facebook", "meta", "instagram", "whatsapp", "me", "gusta", "like",
             "comentar", "comment", "compartir", "share", "ver", "mas", "see",
             "more", "publicidad", "advertisement"}
    return bool(words) and all(w in noise or w.isdigit() for w in words)


def context_ok(expect):
    if not expect or is_boilerplate_expect(expect):
        return True
    t = cxtlib.cdp(CXT, TAB, JS_POST_TEXT) or ""
    return cxtlib.norm(expect) in cxtlib.norm(t)


def share_url_group(url, g, dry, text, expect=""):
    global EXPECT
    # Expect basuriento (p. ej. "facebook facebook..." del visor de fotos) no
    # debe bloquear el fallback del post visible en la pestaña dedicada.
    EXPECT = "" if is_boilerplate_expect(expect) else (expect or "")
    if url:
        log("abro la publicacion: %s" % url)
        goto(url)
        ok = False
        for _ in range(20):
            if cxtlib.cdp(CXT, TAB, js_find_share(EXPECT)):
                ok = True
                break
            sleep(250)
        if not ok:
            cur = cxtlib.cdp(CXT, TAB, "location.href") or ""
            base = cur.split("#")[0].split("?")[0]
            log("aun no veo el post; reintento por permalink: %s" % (base or url))
            goto(base or url)
            for _ in range(20):
                if cxtlib.cdp(CXT, TAB, js_find_share(EXPECT)):
                    ok = True
                    break
                sleep(250)
        if not ok:
            log("ERROR: no pude confirmar la publicacion (identity)")
            return {"ok": False, "stage": "identity"}
    elif not context_ok(expect):
        log("ERROR: el post visible no coincide con '%s' (identity)" % expect)
        return {"ok": False, "stage": "identity"}
    return share_one(g, dry, text)


def share_one(g, dry, text=""):
    for attempt in range(1, 4):
        log("  intento %d/3: abrir menu Compartir" % attempt)
        if not open_share():
            log("  no se abrio el menu Compartir")
            sleep(1200)
            continue
        log("  eligiendo 'Compartir en un grupo'")
        if not open_group_option():
            log("  no aparecio el selector de grupos")
            close_dialog()
            sleep(1200)
            continue
        log("  buscando el grupo: %s" % g)
        if not search_and_select(g):
            log("  no encontre el grupo en el buscador")
            close_dialog()
            sleep(1200)
            continue
        log("  pulsando Publicar (simulacro=%s)" % dry)
        r = submit(dry, text)
        log("  resultado: %s" % json.dumps(r, ensure_ascii=False))
        return r
    log("  AGOTADO: no se pudo completar")
    return {"ok": False, "stage": "retry-exhausted"}


def load_groups():
    return cxtlib.load_json(os.path.join(cxtlib.resolve_dir(), "groups.json")).get("groups", [])


def select_groups(ids, filt, limit):
    if ids:
        return [g.strip() for g in ids.split(",") if g.strip()]
    kws = [cxtlib.norm(x) for x in (filt or "").split(",") if x.strip()]
    if not kws:
        return []
    out = []
    for g in load_groups():
        n = cxtlib.norm(g.get("name", ""))
        if any(k in n for k in kws):
            out.append(g.get("name") or g.get("id"))
    if limit and limit > 0:
        out = out[:limit]
    return out


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
    cxtlib.write_status(STATUS, st)
    log("=== COMPARTIR POST: %d grupos | simulacro=%s | identificar=%r ===" % (len(groups), dry, cfg.get("expect") or ""))
    if not groups:
        log("sin grupos (revisa el filtro o los ids)")
        st.update(running=False, error="sin grupos (revisa filter/ids)", finished_at=int(time.time() * 1000))
        cxtlib.write_status(STATUS, st)
        return

    import signal
    def on_term(*_):
        st.update(running=False, current="", error="detenido", finished_at=int(time.time() * 1000))
        cxtlib.write_status(STATUS, st)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    global TAB
    try:
        TAB = cxtlib.ensure_tab(CXT, TAB)
        for i, g in enumerate(groups):
            st["current"] = g
            st["done"] = i
            cxtlib.write_status(STATUS, st)
            log("[%d/%d] %s" % (i + 1, len(groups), g))
            step = {"group": g}
            step.update(share_url_group(url, g, dry, text, cfg.get("expect") or ""))
            if step.get("ok") and not dry:
                st["published"] = st.get("published", 0) + 1
            st["results"].append(step)
            st["done"] = i + 1
            cxtlib.write_status(STATUS, st)
            if i < len(groups) - 1:
                close_dialog()
                sleep(int(delay * 1000))
    finally:
        st.update(running=False, current="", finished_at=int(time.time() * 1000))
        cxtlib.write_status(STATUS, st)
    log("=== FIN: %d/%d publicados ===" % (st.get("published", 0), len(groups)))


def main():
    global TAB
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--expect", default="")
    ap.add_argument("--delay", type=float, default=10)
    ap.add_argument("--dry", action="store_true", default=True)
    ap.add_argument("--no-dry", dest="dry", action="store_false")
    a = ap.parse_args()
    if a.cmd:
        run_from_cmd(a.cmd)
        return
    if not a.ids:
        sys.exit("usa --ids o --cmd")
    TAB = cxtlib.ensure_tab(CXT, TAB)
    groups = [g.strip() for g in a.ids.split(",") if g.strip()]
    results = []
    log("=== COMPARTIR POST: %d grupos | simulacro=%s ===" % (len(groups), a.dry))
    for i, g in enumerate(groups):
        log("[%d/%d] %s" % (i + 1, len(groups), g))
        step = {"group": g}
        step.update(share_url_group(a.url, g, a.dry, a.text, a.expect))
        results.append(step)
        if i < len(groups) - 1:
            close_dialog()
            sleep(int(max(0.0, a.delay) * 1000))
    log("=== FIN ===")
    print(json.dumps({"dry": a.dry, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
