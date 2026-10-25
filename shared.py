#!/usr/bin/env python3
"""shared.py - motor COMUN de compartir para share_post.py y groups_share.py.

Esperas basadas en CONDICION (sondeo 0.08-0.12 s) en lugar de sleeps fijos: el
unico sleep fijo que se conserva es el `delay` entre grupos que configura el
usuario (y ticks <=50 ms). Registra milisegundos por paso en share_trace.jsonl,
recortada a las ultimas 2000 lineas cuando supera 2 MB. Solo stdlib.

Expone:
  share_to_targets(post_url, targets, dry=True, text="", expect="", delay=None,
                   tab=None, cxt=None) -> list[dict]
  share_target(cxt, tab, post_url, group, dry, text, expect) -> dict  (un grupo)
  post_in_group(cxt, tab, g, text, dry=False) -> dict                 (compositor)
  y las primitivas del flujo (open_share, open_group_option, type_search,
  search_and_select, submit, close_share_dialog, goto_post, composer_rect...).

allow: SIZE_OK - unico motor por mandato (3 archivos); el grueso son constantes
JS verbatim + dos orquestaciones (dialogo nativo y compositor), no logica.
"""
import base64
import json
import os
import re
import time
from urllib.parse import quote_plus

import cxtlib
import fastmode

TICK = 0.1              # sondeo por defecto del motor JS (0.08-0.12 s)
JS_TICK = 0.08          # sondeo corto de las primitivas del dialogo
DF_TICK = 0.12          # sondeo de las primitivas que usan deepfind (mas caro)
TRACE = os.path.join(cxtlib.resolve_dir(), "share_trace.jsonl")
TRACE_MAX_BYTES = 2 * 1024 * 1024
TRACE_KEEP = 2000

# Textos de UI del compositor (localizacion), mismos defaults que groups_share.
COMPOSER_TEXT = os.environ.get("CXT_SHARE_COMPOSER_TEXT") or "escribe algo"
PUBLISH_TEXT = os.environ.get("CXT_SHARE_PUBLISH_TEXT") or "publicar"
SELL_TEXT = os.environ.get("CXT_SHARE_SELL_TEXT", "vender algo")
# Needles del compositor de venta (case- y acento-insensible).
SELL_NEEDLES = tuple(dict.fromkeys(
    cxtlib.norm(x) for x in (SELL_TEXT, "vender algo", "vende algo") if cxtlib.norm(x)))


class CliError(RuntimeError):
    """El CLI cxt fallo (p. ej. extension offline): hay que propagarlo, no vaciar."""


# ------------------------------------------------------------------- core
def now_ms():
    return int(time.time() * 1000)


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def trace(group, step, ms, ok, reason="", stats=None, extra=None):
    """Anexa un paso a share_trace.jsonl; recorta el fichero si supera 2 MB.

    `stats` (opcional) trae tries/hit/timed_out de la espera decisiva del paso:
    asi la traza distingue DETECCION (hit=True) de AGOTAR EL TIMEOUT (timed_out).
    `extra` agrega senales nuevas (dialogs_before/after, click_hit, proof...) sin
    tocar las claves historicas."""
    st = stats or {}
    line = {"ts": now_ms(), "group": group, "step": step, "ms": int(ms),
            "ok": bool(ok), "tries": int(st.get("tries", 0)),
            "hit": bool(st.get("hit", False)),
            "timed_out": bool(st.get("timed_out", False)),
            "reason": reason or "", "mode": fastmode.tag()}
    if extra:
        line.update(extra)
    line = json.dumps(line, ensure_ascii=False)
    try:
        with open(TRACE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(TRACE) > TRACE_MAX_BYTES:
            with open(TRACE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(TRACE, "w", encoding="utf-8") as f:
                f.writelines(lines[-TRACE_KEEP:])
    except OSError as e:
        print("trace err %s: %s" % (TRACE, e), flush=True)


def poll_js(cxt, tab, expr, timeout=4.0, tick=TICK, step=None):
    """Sondea una expresion JS hasta que sea 'truthy' o venza el timeout.

    Devuelve (valor|None, info) con info={tries,hit,timed_out}; si vence y hay
    `step`, deja una linea en stderr para no confundir timeout con deteccion."""
    tries = 0
    t0 = time.time()
    while True:
        tries += 1
        v = cxtlib.cdp(cxt, tab, expr)
        if v:
            return v, {"tries": tries, "hit": True, "timed_out": False}
        if time.time() - t0 >= timeout:
            el = time.time() - t0
            if step:
                print("poll timeout %s tras %.1fs (tries=%d)" % (step, el, tries), flush=True)
            return None, {"tries": tries, "hit": False, "timed_out": True}
        time.sleep(tick)


def poll_js_settle(cxt, tab, expr, timeout=4.0, tick=TICK, step=None):
    """Como poll_js pero completando transiciones en cada intento.

    Necesario cuando la condicion depende de que un modal termine de entrar o
    salir: con la ventana tapada la transicion no avanza sola."""
    tries = 0
    t0 = time.time()
    while True:
        tries += 1
        settle(cxt, tab)
        v = cxtlib.cdp(cxt, tab, expr)
        if v:
            return v, {"tries": tries, "hit": True, "timed_out": False}
        if time.time() - t0 >= timeout:
            if step:
                print("poll timeout %s tras %.1fs (tries=%d)" % (step, time.time() - t0, tries),
                      flush=True)
            return None, {"tries": tries, "hit": False, "timed_out": True}
        time.sleep(tick)


# --------------------------------------------------------------- JS helpers
JS_VISIBLE = "const vis=(e)=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>4&&r.height>4&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0';};"

# vis() mira solo el nodo: Facebook deja CLONES con un ancestro opacity:0 que
# pasan ese filtro (y esconden el "Publicar" real). eff() recorre la cadena.
JS_EFF = ("const eff=(e)=>{for(let x=e;x&&x!==document.documentElement;x=x.parentElement){"
          "const s=getComputedStyle(x);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;"
          "if(x.hasAttribute&&x.hasAttribute('inert'))return false;}"
          "const r=e.getBoundingClientRect();"
          "return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;};")

JS_SCROLL_STEP = "(()=>{const s=document.scrollingElement||document.documentElement;s.scrollBy(0,420);window.scrollBy(0,420);return true;})()"


def js_find_share(expect=""):
    e = json.dumps(expect or "")
    return "(async()=>{" + JS_VISIBLE + """
 const norm=(s)=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
 // El needle puede venir con espacios ("barbershop luxury") y el DOM traer
 // "Barber Shop Luxury": comparar sin separadores evita falsos negativos.
 const squash=(s)=>norm(s).replace(/[^a-z0-9]+/g,'');
 const w=squash(""" + e + """);
 // El control Compartir real usa un aria-label LARGO ("Share, send this to
 // friends or post it on your profile."): casar por PREFIJO (misma regla que
 // content.js) y excluir las opciones del menu nativo ("Share to a group"...).
 const SHARE_RE=/^(share|compartir|comparte|share this|comparte esto)\\b/i;
 const MENU=['compartir en un grupo','share to a group','compartir ahora','share now'];
 const isShare=(a)=>{const t=(a||'').trim();if(!SHARE_RE.test(t))return false;
   if(MENU.indexOf(t.toLowerCase())>=0)return false;
   if(/^(share|compartir)\\s+(settings|options|permissions|ajustes|opciones|preferencias)\\b/i.test(t))return false;
   return true;};
 const seen=new Set();
 const all=[...document.querySelectorAll('[aria-label]')]
   .filter(el=>isShare(el.getAttribute('aria-label')||''))
   .map(el=>el.closest('div[role=button],a[role=button],button')||el)
   .filter(el=>{if(seen.has(el))return false;seen.add(el);return true;})
   .filter(vis);
  if(!all.length) return null;
  // Si hay una PUBLICACION abierta como ventana flotante, el boton del feed
  // de fondo queda a la vista pero NO es el post: aceptar solo el Compartir
  // que vive dentro de ese modal. Asi no se comparte el post equivocado.
  const dialogs=[...document.querySelectorAll('div[role=dialog]')].filter(vis);
  const modals=dialogs.filter(d=>d.querySelector('[role=article]')||
    /publicaci\\u00f3n de|post by/i.test((d.innerText||'').slice(0,3000)));
  const matching=modals.filter(m=>!w||squash(m.innerText||'').includes(w));
  if(modals.length && !matching.length)
    return {reason:'post-no-coincide',scope:'dialog'};
  // El post puede estar en dos capas (fondo full-screen + tarjeta). El boton
  // Compartir vive en una sola: elegir la capa que realmente lo contiene.
  const modal=matching.find(m=>all.some(x=>m.contains(x)))||
    matching[matching.length-1]||null;
  let el=null, scope='none', box=null;
  if(modal){
    const inside=all.filter(x=>modal.contains(x));
    el=inside.length?inside[inside.length-1]:null;
    if(!el) return {reason:'sin-share-en-modal',scope:'dialog'};
    scope='dialog'; box=modal;
  } else {
    // Sin modal: comportamiento historico (pagina dedicada, menu ya abierto).
    const dlg=dialogs.find(d=>{
      if(d.querySelector('[role=article]')) return true;
      return [...d.querySelectorAll('[aria-label]')].some(x=>isShare(x.getAttribute('aria-label')||''));
    });
    if(dlg){ box=dlg; }
    else if(w){ box=[...document.querySelectorAll('[role=article]')].find(a=>squash(a.innerText||'').includes(w))||null; }
    else { box=[...document.querySelectorAll('[role=article]')].find(a=>a.querySelector('[aria-label^="Share"],[aria-label^="Compartir"]'))||null; }
    if(box){
      if(w && !squash(box.innerText||'').includes(w)) return null;
      el=all.find(x=>box.contains(x))||null;
      scope=dlg?'dialog':'article';
    }
    if(!el){
      // Fallback: post visible de la pestaña dedicada o permalink de foto/video.
      const pm=/\\/posts\\/|\\/permalink|\\/share\\/p\\/|\\/photo|\\/videos\\//.test(location.href);
      if(!w || pm){
        const main=document.querySelector('[role=main]');
        let cands=(main?all.filter(e=>main.contains(e)):[]);
        if(!cands.length) cands=all.slice();
        cands.sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y);
        el=cands[0]||null; scope='fallback';
      }
      if(!el) return null;
    }
  }
  // El action bar del modal nace DEBAJO del viewport. scrollIntoView es
  // asincrono: leer el rect de inmediato devolvia la Y vieja y el click caia
  // en el feed de fondo. Esperar el reflow y verificar el hit-test.
  el.scrollIntoView({block:'center',inline:'nearest'});
  await new Promise(r=>setTimeout(r,550));
  const r=el.getBoundingClientRect();
  const x=Math.round(r.x+r.width/2), y=Math.round(r.y+r.height/2);
  if(r.width<8||r.height<8) return {reason:'share-cero',scope:scope,x:x,y:y};
  if(x<0||y<0||x>=innerWidth||y>=innerHeight)
    return {reason:'share-fuera-vista',scope:scope,x:x,y:y};
  const top=document.elementFromPoint(x,y);
  if(!top||!(top===el||el.contains(top)))
    return {reason:'share-cubierto',scope:scope,x:x,y:y};
  return {x:x,y:y,scope:scope,w:Math.round(r.width),h:Math.round(r.height)};})()"""


def share_point_ok(ev):
    """True solo si el sondeo devolvio un punto pulsable, no un motivo de aborto."""
    return isinstance(ev, dict) and ev.get("x") is not None


def js_share_context(expect="", post_url=""):
    """Evidencia de que el compositor activo contiene el post original."""
    return "(()=>{" + JS_VISIBLE + r"""
 const squash=(s)=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'')
   .toLowerCase().replace(/[^a-z0-9]+/g,'');
 const want=squash(""" + json.dumps(expect or "") + r""");
 const postUrl=""" + json.dumps(post_url or "") + r""";
 const pubs=[...document.querySelectorAll('div[role=button],button,a[role=button]')].filter(vis)
   .filter(b=>/^(publicar|post)$/i.test((b.textContent||'').trim()));
 const b=pubs[pubs.length-1]||null;
 if(!b) return {reason:'sin-boton'};
 let root=b.closest('div[role=dialog]');
 if(!root){let e=b;for(let i=0;i<10&&e.parentElement;i++){e=e.parentElement;
   const r=e.getBoundingClientRect();
   if(r.width>=300&&r.width<=900&&r.height>=150&&r.height<=1200){root=e;break;}}}
 if(!root) root=b.parentElement||b;
 const text=(root.innerText||'').replace(/\s+/g,' ').trim();
 const hrefs=[...root.querySelectorAll('a[href]')].map(a=>a.getAttribute('href')||'');
 let postId='';
 let m=postUrl.match(/pfbid[0-9A-Za-z]+/); if(m) postId=m[0];
 if(!postId){m=postUrl.match(/\/posts\/(\d+)/); if(m) postId=m[1];}
 if(!postId){m=postUrl.match(/[?&](?:story_fbid|fbid)=(\d+)/); if(m) postId=m[1];}
 if(!postId){m=postUrl.match(/\/share\/p\/([^/?#]+)/); if(m) postId=m[1];}
 const link=postId?hrefs.some(h=>h.indexOf(postId)>=0)
   :hrefs.some(h=>/\/posts\/|\/permalink|\/share\/p\//.test(h));
 const needle=!!want&&squash(text).indexOf(want)>=0;
 const arts=[...root.querySelectorAll('[role=article]')].filter(vis).length;
 return {reason:(link||needle)?'ok':'composer-sin-post',link:link,needle:needle,
   articles:arts,postId:postId,text:text.slice(0,800)};})()"""


def classify_share_context(ev):
    """Clasifica la evidencia del compositor: solo enlace o texto del post."""
    if not isinstance(ev, dict):
        return False, "composer-sin-post"
    if ev.get("link") or ev.get("needle"):
        return True, "ok"
    return False, "composer-sin-post"


JS_POST_TEXT = "(()=>{" + JS_VISIBLE + """
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).find(d=>d.querySelector('[role=article],[aria-label^="Share"],[aria-label^="Compartir"]'));
 const root=dlg||document.querySelector('[role=main]')||document.body;
 return (root.innerText||'').slice(0,4000);})()"""


JS_GROUP_OPTION = "(()=>{" + JS_VISIBLE + """
 const want=['share to a group','compartir en un grupo'];
 for(const el of document.querySelectorAll('[aria-label]')){
   const a=(el.getAttribute('aria-label')||'').trim().toLowerCase();
   if(want.includes(a)&&vis(el)){const r=el.getBoundingClientRect();
     if(r.width>30) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}
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

# El tile "Grupo" abre el buscador con .click() nativo. La secuencia sintetica
# completa de JS_CLICK_AT cierra la hoja de compartir en esta version de FB.
JS_GROUP_CLICK = "(async()=>{" + JS_VISIBLE + """
 const want=['share to a group','compartir en un grupo'];
 for(const el of document.querySelectorAll('[aria-label]')){
   const a=(el.getAttribute('aria-label')||'').trim().toLowerCase();
   const r=el.getBoundingClientRect();
   const dlg=el.closest('div[role=dialog]');
   if(!want.includes(a)||!vis(el)||r.width<=30||!dlg||!vis(dlg)) continue;
   const txt=(dlg.innerText||'').toLowerCase();
   if(!txt.includes('compartir')&&!txt.includes('share')) continue;
   el.scrollIntoView({block:'center',inline:'center'});
   await new Promise(res=>setTimeout(res,180));
   const r2=el.getBoundingClientRect();
   if(r2.top<0||r2.bottom>innerHeight) return false;
   el.click();
   return true;
 }
 return false;})()"""

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

JS_SCROLL_MODAL = "(()=>{const el=[...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.x>200&&r.x<745&&r.y>40&&r.y<645&&r.height>250&&(s.overflowY==='auto'||s.overflowY==='scroll');})[0];if(el){el.scrollBy(0,360);return true;}return false;})()"

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

# El picker de grupos no tiene checkbox ni boton "Publicar": la fila solo
# SELECCIONA el grupo y el paso real es "Compartir ahora". Pulsar la fila dos
# veces abre el compositor NORMAL del grupo y publica un post de solo texto.
JS_PICKER_SHARE = "(()=>{" + JS_VISIBLE + """
 const inp=[...document.querySelectorAll('input[type=search]')].filter(vis).find(i=>{
   const a=((i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();
   return /grupo|group/.test(a);});
 if(!inp) return null;
 const dlg=inp.closest('div[role=dialog]');
 if(!dlg) return null;
 const cands=[...dlg.querySelectorAll('div[role=button],button,a[role=button]')].filter(vis)
   .filter(b=>{const t=(b.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase();
     return t==='compartir ahora'||t==='share now';});
 if(!cands.length) return null;
 const b=cands[cands.length-1];
 b.scrollIntoView({block:'center'});
 const r=b.getBoundingClientRect();
 return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),
   w:Math.round(r.width),h:Math.round(r.height),
   disabled:b.getAttribute('aria-disabled')==='true'||!!b.disabled};})()"""

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

# Audiencia REAL del compositor. El chip de privacidad es la unica prueba de que
# el compositor publicara en un GRUPO (el dialogo nativo dice solo "Publico").
# Localizado: el aria-label es "Editar privacidad. Compartir con ..." (es).
JS_AUDIENCE = "(()=>{" + JS_VISIBLE + JS_EFF + """
 const out=[];
 for(const e of document.querySelectorAll('[aria-label]')){
   const a=(e.getAttribute('aria-label')||'').trim();
   if(!/^(editar privacidad|edit privacy)\\b/i.test(a)) continue;
   if(!vis(e)||!eff(e)) continue;
   out.push(a+' | '+(e.textContent||'').trim());
 }
 return {chips:out.slice(0,6), group: out.some(t=>/grupo|group/i.test(t))};})()"""

# Boton FINAL del compositor (Publicar/Post/Share to selected), ACOTADO al
# contenedor que posee el compositor. En Facebook el compositor de grupo se
# pinta en una capa propia (no vive bajo div[role=dialog]): el contenedor se
# calcula como el ancestro comun mas bajo (LCA) del boton y del chip de
# audiencia de grupo. Sin ese ancla NO se pulsa: un "Publicar" de otra capa
# (tapado por un dialogo viejo) haria que el click fiel cayera en el backdrop.
# El punto se valida ademas con elementFromPoint (debe ser el boton o hijo).
JS_SUBMIT = "(()=>{" + JS_VISIBLE + JS_EFF + """
 const rect=(e)=>{const r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)};};
 const SEL='div[role=button],button,a[role=button]';
 const isPub=(t)=>/^share to selected/i.test(t)||/^(publicar|post)$/i.test(t);
 const geo=[...document.querySelectorAll(SEL)].filter(vis).filter(b=>{
   const r=b.getBoundingClientRect();return r.width>=120&&r.height>=26&&isPub((b.textContent||'').trim());});
 const cands=geo.filter(eff);
 if(!cands.length) return geo.length?{reason:'invisible',n:geo.length}:null;
 const chips=[...document.querySelectorAll('[aria-label]')].filter(vis).filter(eff)
   .filter(e=>/^(editar privacidad|edit privacy)\\b/i.test((e.getAttribute('aria-label')||'').trim()));
 const groupChips=chips.filter(e=>/grupo|group/i.test(((e.getAttribute('aria-label')||'')+' '+(e.textContent||'')).trim()));
 const chip=groupChips.length?groupChips[groupChips.length-1]:null;
 const path=(e)=>{const a=[];for(let x=e;x&&x!==document.body;x=x.parentElement)a.push(x);return a;};
 let root=null,b=null;
 if(chip){
   const anc=path(cands[cands.length-1]);
   for(let x=chip;x;x=x.parentElement){if(anc.indexOf(x)>=0){root=x;break;}}
   const inRoot=cands.filter(c=>root&&root.contains(c));
   b=inRoot.length?inRoot[inRoot.length-1]:null;
 }
 if(!b&&!chip){
   const last=cands[cands.length-1];
   for(let x=last.parentElement;x&&x!==document.body;x=x.parentElement){
     const rr=x.getBoundingClientRect();
     if(rr.width>=300&&rr.width<=900&&rr.height>=150&&rr.height<=900){root=x;break;}
   }
   b=last;
 }
 if(!b) return {reason:'sin-contenedor',n:cands.length,chips:chips.length};
 const aud=chip?((chip.getAttribute('aria-label')||'')+' | '+(chip.textContent||'').trim()):'';
 if(!chip){
   const t=root?((root.innerText||'').slice(0,600)):'';
   if(!/grupo p\\u00fablico|public group/i.test(t))
     return {reason:'sin-grupo',label:(b.textContent||'').trim(),audience:aud.slice(0,160),root:root?rect(root):null};
 }
 const r=b.getBoundingClientRect();
 const base={x:Math.round(r.x+r.width/2),y:Math.round(r.y+Math.round(r.height*0.4)),
             label:(b.textContent||'').trim(),w:Math.round(r.width),h:Math.round(r.height),
             audience:aud.slice(0,160),root:root?rect(root):null};
 if((b.getAttribute('aria-disabled')||'')==='true'||b.disabled===true) return Object.assign({reason:'disabled'},base);
 if(r.width<4||r.height<4) return Object.assign({reason:'cero'},base);
 if(base.x<0||base.y<0||base.x>=innerWidth||base.y>=innerHeight) return Object.assign({reason:'fuera-vista'},base);
 const top=document.elementFromPoint(base.x,base.y);
 if(!top||!(top===b||b.contains(top))) return Object.assign({reason:'cubierto'},base);
 if(root){const rr=root.getBoundingClientRect();
   if(rr.width>1400||rr.height>1200) return Object.assign({reason:'contenedor-grande'},base);}
 return base;})()"""

# Pila de superficies conocidas: menu/picker nativo, compositor de grupo y
# cualquier "Publicar" ancho. Se usa para LIMPIAR antes de empezar: un dialogo
# viejo tapando el compositor hace que el click fiel caiga en el backdrop.
JS_STACK = "(()=>{" + JS_VISIBLE + JS_EFF + """
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).filter(eff).filter(d=>{
   const t=(d.innerText||'').toLowerCase();const al=(d.getAttribute('aria-label')||'').toLowerCase();
   return /compartir|share|crear publicaci|create post/.test(t)||/crear publicaci|create post/.test(al);});
 const comp=[...document.querySelectorAll('[aria-label="Crear publicación"],[aria-label="Create post"]')].filter(vis).filter(eff)
   .filter(e=>{const r=e.getBoundingClientRect();return r.width>250&&r.height>30;});
 const pubs=[...document.querySelectorAll('div[role=button],button,a[role=button]')].filter(vis).filter(eff)
   .filter(b=>{const r=b.getBoundingClientRect();return r.width>120&&/^(publicar|post)$/i.test((b.textContent||'').trim());});
 return {any:dlg.length>0||comp.length>0||pubs.length>0,dialogs:dlg.length,
         composer:comp.length,submit:pubs.length,
         labels:dlg.slice(0,4).map(d=>(d.innerText||'').replace(/\\s+/g,' ').slice(0,80))};})()"""

JS_STACK_CLEAN = "(()=>{const s=" + JS_STACK + ";return !s.any;})()"

# X de cierre del dialogo SUPERIOR, solo si el punto es realmente suyo.
JS_TOP_CLOSE = "(()=>{" + JS_VISIBLE + """
 const re=/^(cerrar|close|cerrar el cuadro de diálogo del editor|close post dialog|close dialog)$/i;
 const xs=[...document.querySelectorAll('[aria-label]')].filter(vis)
   .filter(e=>re.test((e.getAttribute('aria-label')||'').trim()));
 for(let i=xs.length-1;i>=0;i--){const e=xs[i];const r=e.getBoundingClientRect();
   if(r.width<8||r.height<8)continue;
   const x=Math.round(r.x+r.width/2),y=Math.round(r.y+r.height/2);
   const t=document.elementFromPoint(x,y);
   if(t&&(t===e||e.contains(t)))return {x:x,y:y,label:(e.getAttribute('aria-label')||'').slice(0,40)};}
 return null;})()"""

# Cierra solo menu/picker/compositor: si hay un post en modal, su X se conserva.
JS_TOP_NONPOST_CLOSE = "(()=>{" + JS_VISIBLE + """
 const dialogs=[...document.querySelectorAll('div[role=dialog]')].filter(vis);
 const posts=dialogs.filter(d=>d.querySelector('[role=article]')||
   /publicaci\\u00f3n de|post by/i.test((d.innerText||'').slice(0,3000)));
 const others=dialogs.filter(d=>!posts.includes(d));
 if(!others.length) return null;
 const re=/^(cerrar|close|cerrar el cuadro de diálogo del editor|close post dialog|close dialog)$/i;
 // Recorre TODAS las capas no-post, de arriba abajo: la X de una puede estar
 // tapada por otra capa (menu/header) y la anterior version se rendia con la
 // primera. Solo se acepta un cierre que pertenezca a una capa no-post.
 const cands=[];
 for(let j=others.length-1;j>=0;j--){
   const dlg=others[j];
   for(const e of dlg.querySelectorAll('[aria-label],div[role=button],button')){
     if(!vis(e))continue;
     const a=(e.getAttribute('aria-label')||'').trim();
     const tx=(e.textContent||'').trim();
     if(!(re.test(a)||re.test(tx)))continue;
     const r=e.getBoundingClientRect();
     if(r.width<8||r.height<8)continue;
     cands.push({e:e,x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),
                 label:(a||tx).slice(0,40)});
   }
 }
 for(const c of cands){
   const t=document.elementFromPoint(c.x,c.y);
   if(!t)continue;
   const top=t.closest('[aria-label],div[role=button],button')||t;
   const owner=others.find(d=>d.contains(top));
   if(!owner)continue;   // el elemento superior es del post: no tocarlo
   const lab=(top.getAttribute('aria-label')||'').trim();
   const tx=(top.textContent||'').trim();
   if(t===c.e||c.e.contains(t)||re.test(lab)||re.test(tx))
     return {x:c.x,y:c.y,label:c.label};
 }
 return null;})()"""

# Cuenta solo capas no-post (menu/picker/compositor); el post modal no cuenta.
JS_NONPOST_COUNT = "(()=>{" + JS_VISIBLE + """
 const dialogs=[...document.querySelectorAll('div[role=dialog]')].filter(vis);
 const posts=dialogs.filter(d=>d.querySelector('[role=article]')||
   /publicaci\\u00f3n de|post by/i.test((d.innerText||'').slice(0,3000)));
 const others=dialogs.filter(d=>!posts.includes(d));
 return others.length;})()"""

# Confirmacion de descarte ("Descartar publicacion") si el cierre la abre.
JS_DISCARD = "(()=>{" + JS_VISIBLE + """
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).find(d=>{
   const t=(d.innerText||'').toLowerCase();return /descartar|discard|salir sin publicar|leave post/i.test(t);});
 if(!dlg) return null;
 const re=/^(descartar|discard|salir|leave|eliminar|delete)$/i;
 const bs=[...dlg.querySelectorAll('div[role=button],button,a[role=button]')].filter(vis)
   .filter(b=>re.test(((b.textContent||'').trim()||(b.getAttribute('aria-label')||'').trim())));
 for(let i=bs.length-1;i>=0;i--){const b=bs[i];const r=b.getBoundingClientRect();
   const x=Math.round(r.x+r.width/2),y=Math.round(r.y+r.height/2);
   const t=document.elementFromPoint(x,y);
   if(t&&(t===b||b.contains(t)))return {x:x,y:y,label:(b.textContent||'').trim().slice(0,40)};}
 return null;})()"""

# Click SINTETICO (mismo realClick que la op de 'click' de la extension): se
# ejecuta en la pagina (~60 ms) en vez de Input.dispatchMouseEvent (~5 s aqui,
# porque Chrome retrasa el ack del raton en esta ventana).
JS_CLICK_AT = """(()=>{const el=document.elementFromPoint(%d,%d); if(!el) return false;
 const o={bubbles:true,cancelable:true,composed:true,view:window};
 for(const t of ['pointerover','mouseover','pointerdown','mousedown','pointerup','mouseup','click']){
   const E=t.indexOf('pointer')===0?PointerEvent:MouseEvent;
   try{el.dispatchEvent(new E(t,o));}catch(_){}}
 try{el.click();}catch(_){}
 return true;})()"""

# Condiciones combinadas (expresiones autocontenidas) para las esperas.
JS_MENU_OR_PICKER = "(" + JS_MENU + ")||(" + JS_PICKER + ")"
JS_SUBMIT_OPEN = ("(()=>{" + JS_EFF + "const b=[...document.querySelectorAll('div[role=button],button,a[role=button]')]"
                  ".filter(b=>{const r=b.getBoundingClientRect();return r.width>=120&&r.height>=26&&"
                  "/^(publicar|post)$/i.test((b.textContent||'').trim());}).filter(eff);"
                  "return b.length>0;})()")
JS_ANY_OPEN = "(" + JS_PICKER + ")||(" + JS_MENU + ")||(" + JS_SUBMIT_OPEN + ")"
JS_NONE_OPEN = "!(" + JS_ANY_OPEN + ")"
# Confirmacion visible tras publicar (algunos grupos dejan el compositor abierto
# con "publicacion pendiente de aprobacion"): vale como exito observado.
JS_PUBLISH_SIGNAL = "(()=>{" + JS_VISIBLE + """
 const re=/(publicaci[oó]n|post)\\s+(creada|enviada|pendiente|publicada)|se ha publicado|published|post (created|submitted|pending)/i;
 for(const e of [...document.querySelectorAll('div[role=alert],div[role=status],div[role=dialog]')].filter(vis)){
   const t=(e.innerText||'').replace(/\\s+/g,' ');if(re.test(t))return t.slice(0,200);}
 return null;})()"""

# Aviso de que FB dejo la publicacion PENDIENTE de aprobacion del admin: NO es
# exito (puede que nunca se publique) ni fallo del motor. Se escanea antes y
# despues del click para poder reportar el estado `pendiente-aprobacion` sin
# cantar victoria ni culpar al script.
JS_PENDING_NOTICE = "(()=>{" + JS_VISIBLE + r"""
 const re=/(pendiente\s+de\s+aprobaci[oó]n|aprobaci[oó]n\s+(del|de un)\s+administrador|en\s+espera\s+de\s+aprobaci[oó]n|pending\s+approval|waiting\s+for\s+approval)/i;
 for(const e of [...document.querySelectorAll('div[role=alert],div[role=status],div[role=dialog]')].filter(vis)){
   const t=(e.innerText||'').replace(/\s+/g,' ');if(re.test(t))return t.slice(0,200);}
 return null;})()"""

# APROBACION por admin: un grupo deja la publicacion pendiente y dias/horas
# despues un admin la aprueba. El aviso dentro del dialogo (JS_PENDING_NOTICE)
# no siempre sale; la senal REAL es /notifications (notif_t=group_post_approved
# o ref=notif). Devuelve {href, items:[{url,group,text}]} y nunca lanza.
JS_APPROVALS = r"""(()=>{
 try{
  const out=[],seen={};
  const clean=(s)=>((s||'')+'').replace(/\s+/g,' ').trim();
  const ap=/aprob(?:ó|o)\s+tu\s+publicaci[oó]n\s+en\s+/i;
  const groupOf=(t)=>{const m=clean(t).match(/aprob(?:ó|o)\s+tu\s+publicaci[oó]n\s+en\s+(.+)/i);
    return m?clean(m[1]).slice(0,160):'';};
  const rowText=(a)=>{let e=a;
    for(let i=0;i<8&&e;i++){
      const t=clean(e.innerText||e.textContent||'');
      if(t.length>=20&&t.length<=600)return t;
      e=e.parentElement;}
    return clean(a.innerText||a.textContent||'');};
  const add=(url,group,text)=>{
    url=clean(url);
    if(!url||seen[url]||out.length>=40)return;
    if(!/\/groups\/[^\/]+\/posts\/[0-9]+/.test(url))return;
    seen[url]=1;
    out.push({url:url.slice(0,400),group:clean(group).slice(0,160),
              text:clean(text).slice(0,200)});};
  for(const a of [...document.querySelectorAll('a[href]')]){
    const href=a.getAttribute('href')||'';
    if(!/\/groups\/[^\/]+\/posts\/[0-9]+/.test(href))continue;
    const rt=rowText(a);
    if(!(/notif_t=group_post_approved/.test(href)||/[?&]ref=notif/.test(href)||ap.test(rt)))continue;
    add(a.href||href,groupOf(rt),rt);}
  for(const e of [...document.querySelectorAll('div[role=article],div[role=listitem],li')]){
    const t=clean(e.innerText||'');
    if(!ap.test(t)||t.length>600)continue;
    const a=e.querySelector('a[href*="/groups/"]');
    if(!a)continue;
    const href=a.getAttribute('href')||'';
    if(!/\/groups\/[^\/]+\/posts\/[0-9]+/.test(href))continue;
    add(a.href||href,groupOf(t),t);}
  return {href:location.href,items:out};
 }catch(_){return {href:'',items:[]};}
})()"""

# Permalink de una aprobacion: mismas senales que verif_aprobado.py, medidas
# en vivo sobre el post real aprobado. El autor esperado y el needle llegan YA
# normalizados (cxtlib.norm) y se comparan dentro de la pagina: aqui no se
# hardcodea ninguna identidad.
JS_APPROVAL_POST = r"""(()=>{
const author=__AUTHOR__,needle=__NEEDLE__;
const norm=(s)=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'')
  .replace(/[\u200b-\u200f\u2060\ufeff\u00ad]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
const vis=(el)=>{if(!el)return false;const r=el.getBoundingClientRect();
  return r.width>0&&r.height>0&&getComputedStyle(el).visibility!=='hidden';};
const count=(hay,n)=>{if(!n)return 0;let i=-1,c=0;
  while((i=hay.indexOf(n,i+1))>=0)c++;return c;};
const body=((document.body&&document.body.innerText)||'').slice(0,60000);
const nb=norm(body);
const arts=[...document.querySelectorAll('div[role=article]')].filter(vis);
const hits=needle?count(nb,needle):0;
const err=(body.match(/no est[aá] disponible|contenido no|solo compart[ií]|restringid/gi)||[]).slice(0,3);
return {href:location.href,title:(document.title||'').slice(0,160),
  articles:arts.length,has_author:!!author&&nb.indexOf(author)>=0,
  needle:!!needle&&hits>0,needle_hits:hits,err:err.join(' | ')};})()"""


def js_approval_post(author="", needle=""):
    """Rellena la sonda del permalink con autor/needle ya normalizados (puro)."""
    return (JS_APPROVAL_POST.replace("__AUTHOR__", json.dumps(cxtlib.norm(author or "")))
            .replace("__NEEDLE__", json.dumps(cxtlib.norm(needle or ""))))


# Exito del envio = el compositor ya no esta RENDERIZADO. No vale mirar el
# boton: tras publicar queda un clon oculto que sigue respondiendo al DOM.
JS_COMPOSER_GONE = ("(()=>{const t=document.body.innerText||'';"
                    "return !/crear publicaci[oó]n|create post/i.test(t);})()")

# Conteo REAL de dialogos visibles (TODOS, sin filtrar por texto): la limpieza
# solo puede afirmar "limpio" cuando este numero es 0. El body.innerText y
# JS_STACK no sirven para eso: dejan clones con texto dentro que nunca cuentan.
JS_DIALOG_COUNT = "(()=>{" + JS_VISIBLE + """
 const d=[...document.querySelectorAll('div[role=dialog]')].filter(vis);
 return d.length;})()"""

# Compositor REAL (aria-label) y boton Publicar, ambos efectivamente visibles.
# Sustituye a JS_COMPOSER_GONE (texto del body): el texto sobrevive en clones
# ocultos y hacia que un envio exitoso se leyera como "dialogo abierto".
JS_COMPOSER_COUNT = "(()=>{" + JS_VISIBLE + JS_EFF + """
 const comp=[...document.querySelectorAll('[aria-label="Crear publicación"],[aria-label="Create post"]')]
   .filter(vis).filter(eff).filter(e=>{const r=e.getBoundingClientRect();return r.width>250&&r.height>30;});
 const pubs=[...document.querySelectorAll('div[role=button],button,a[role=button]')]
   .filter(vis).filter(eff).filter(b=>{const r=b.getBoundingClientRect();
     return r.width>=120&&r.height>=26&&/^(publicar|post)$/i.test((b.textContent||'').trim());});
 return {composer:comp.length,submit:pubs.length};})()"""
JS_COMPOSER_CLOSED = ("(()=>{const s=" + JS_COMPOSER_COUNT + ";"
                      "return s.composer===0&&s.submit===0;})()")

# X de cierre del dialogo visible MAS ALTO que NO contiene el boton Publicar.
# Se usa solo cuando el punto del boton esta CUBIERTO: cierra la capa que lo
# tapa y nunca manda Escape (Escape cerraria el propio compositor).
JS_STALE_CLOSE = "(()=>{" + JS_VISIBLE + """
 const btns=[...document.querySelectorAll('div[role=button],button,a[role=button]')]
   .filter(vis).filter(b=>{const r=b.getBoundingClientRect();
     return r.width>=120&&r.height>=26&&/^(publicar|post)$/i.test((b.textContent||'').trim());});
 const b=btns[btns.length-1];
 if(!b) return {n:0,x:null};
 const dlg=[...document.querySelectorAll('div[role=dialog]')].filter(vis).filter(d=>!d.contains(b));
 const re=/^(cerrar|close)$/i;
 for(let i=dlg.length-1;i>=0;i--){
   const xs=[...dlg[i].querySelectorAll('[aria-label]')]
     .filter(x=>re.test((x.getAttribute('aria-label')||'').trim()));
   for(const e of xs){const r=e.getBoundingClientRect();if(r.width<8||r.height<8)continue;
     const x=Math.round(r.x+r.width/2),y=Math.round(r.y+r.height/2);
     const t=document.elementFromPoint(x,y);
     if(t&&(t===e||e.contains(t)))return {x:x,y:y,dialogs:dlg.length};}
 }
 return {n:dlg.length,x:null};})()"""

# Auditoria del punto de click: que elementos hay realmente encima.
JS_POINT_STACK = ("(()=>{const es=document.elementsFromPoint(%d,%d);"
                  "return es.slice(0,8).map(e=>e.tagName+':'+"
                  "((e.innerText||e.textContent||'').trim().slice(0,25)));})()")

# Prueba de ENTREGA del click fiel: registra el mousedown/pointerdown real y
# luego se compara su coordenada con el punto pulsado (cdpclick -> Input events).
JS_ARM_HIT = ("(()=>{window.__cxtHit=null;const h=(ev)=>{window.__cxtHit={"
              "x:Math.round(ev.clientX),y:Math.round(ev.clientY),"
              "tag:(ev.target&&ev.target.tagName)||''};};"
              "window.addEventListener('mousedown',h,true);"
              "window.addEventListener('pointerdown',h,true);return true;})()")
JS_HIT = "(()=>window.__cxtHit||null)()"

JS_READY = "(()=>document.readyState==='complete')()"

# Esta ventana de Chrome no compone frames cuando esta tapada, asi que las
# transiciones de los modales de Facebook se quedan a medias: el compositor de
# grupo se queda en su posicion de entrada (a la derecha y bajo una capa con
# opacity 0). Terminar las animaciones lo deja en su sitio y pulsable (~30 ms).
JS_FINISH_ANIMS = ("(()=>{const a=document.getAnimations();for(const x of a){"
                   "try{x.finish()}catch(e){}}return a.length;})()")


def settle(cxt, tab):
    """Completa las transiciones CSS pendientes de la pagina."""
    return cxtlib.cdp(cxt, tab, JS_FINISH_ANIMS)


def js_select_row(name):
    n = json.dumps(name)
    return "(()=>{" + JS_VISIBLE + """
 const norm=(s)=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
 const tgt=norm(""" + n + """);
 const hit=(t)=>t.includes(tgt)||(tgt.length>6&&tgt.includes(t));
 // Acotado al dialogo del picker (input de busqueda de grupos): una fila del
 // feed de fondo abria el compositor normal del grupo en vez del de compartir.
 const inp=[...document.querySelectorAll('input[type=search]')].filter(vis).find(i=>{
   const a=((i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();
   return /grupo|group/.test(a);});
 const scope=inp?inp.closest('div[role=dialog]'):null;
 if(!scope) return null;
 const q=(sel)=>[...scope.querySelectorAll(sel)].filter(vis);
 for(const b of q('div[role=button]')){
   const cb=b.querySelector('input[type=checkbox]');
   if(cb && hit(norm(b.textContent||''))){const r=cb.getBoundingClientRect();
     if(r.width>2) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),t:(b.textContent||'').trim().slice(0,50)};}
 }
 for(const cb of q('input[type=checkbox]')){
   let e=cb;
   for(let i=0;i<7&&e;i++){e=e.parentElement; if(e&&hit(norm(e.textContent||''))){const r=cb.getBoundingClientRect();
     return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),t:(e.textContent||'').trim().slice(0,50)};}}
 }
 for(const b of q('div[role=button],a[role=button]')){
   const t=norm(b.textContent||'');
   if(hit(t)){const r=b.getBoundingClientRect();
     if(r.width>60&&r.height>20&&r.y>0&&r.y<innerHeight) return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),t:(b.textContent||'').trim().slice(0,50)};}
 }
 return null;})()"""


# ---------------------------------------------------------- flujo: dialogo
def _find_share_scrolling(cxt, tab, expect, timeout=4.0):
    """Busca el boton Compartir sondeando; scrollea entre intentos."""
    t0 = time.time()
    while True:
        f = cxtlib.cdp(cxt, tab, js_find_share(expect))
        if f:
            return f
        cxtlib.cdp(cxt, tab, JS_SCROLL_STEP)
        if time.time() - t0 >= timeout:
            return None
        time.sleep(TICK)


def js_click(cxt, tab, x, y):
    """Pulsa en (x,y) con realClick en la pagina (~60 ms) en vez de cdpclick."""
    return cxtlib.cdp(cxt, tab, JS_CLICK_AT % (int(x), int(y)))



def js_select_group(name):
    """Selecciona el grupo dentro del picker con .click() directo, sin coordenadas."""
    n = json.dumps(name)
    return "(()=>{" + JS_VISIBLE + """
 const norm=(s)=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
 const tgt=norm(""" + n + """);
 const hit=(t)=>t===tgt||t.includes(tgt)||(tgt.length>6&&tgt.includes(t));
 const inp=[...document.querySelectorAll('input[type=search]')].filter(vis).find(i=>{
   const a=((i.getAttribute('aria-label')||'')+' '+(i.placeholder||'')).toLowerCase();
   return /grupo|group/.test(a);});
 const scope=inp?inp.closest('div[role=dialog]'):null;
 if(!scope) return null;
 const q=(sel)=>[...scope.querySelectorAll(sel)].filter(vis);
 for(const b of q('div[role=button]')){
   const cb=b.querySelector('input[type=checkbox]');
   if(cb && /grupo|group/i.test(b.textContent||'') && hit(norm(b.textContent||''))){cb.click();return {t:(b.textContent||'').trim().slice(0,80),checked:cb.checked};}
 }
 for(const cb of q('input[type=checkbox]')){
   let e=cb;
   for(let i=0;i<7&&e;i++){e=e.parentElement;if(e&&/grupo|group/i.test(e.textContent||'')&&hit(norm(e.textContent||''))){cb.click();return {t:(e.textContent||'').trim().slice(0,80),checked:cb.checked};}}
 }
 for(const b of q('div[role=button],a[role=button]')){
   const t=(b.textContent||'').replace(/\\s+/g,' ').trim();
   if(/grupo|group/i.test(t)&&hit(norm(t))){b.click();return {t:t.slice(0,80),clicked:true};}
 }
 return null;})()"""

def _stable_row(cxt, tab, name, timeout=3.0):
    """Espera una fila que casa y esta quieta (mismo rect 2 sondeos seguidos).

    La lista del buscador se pinta con retardo: pulsar la primera coincidencia
    cae sobre una fila que aun se mueve y no selecciona el grupo. Esperar la
    estabilidad es la DETECCION real del paso."""
    prev = None
    tries = 0
    t0 = time.time()
    while True:
        tries += 1
        r = cxtlib.cdp(cxt, tab, js_select_row(name))
        if r and prev and r.get("x") == prev.get("x") and r.get("y") == prev.get("y"):
            return r, {"tries": tries, "hit": True, "timed_out": False}
        prev = r
        if time.time() - t0 >= timeout:
            return r, {"tries": tries, "hit": bool(r), "timed_out": not bool(r)}
        time.sleep(TICK)


def _stable_pt(cxt, tab, expr, timeout=2.0):
    """Espera a que `expr` devuelva un punto QUIETO (mismo x,y 2 sondeos).

    Igual que _stable_row: pulsar con un click sintetico durante la animacion
    de apertura del menu no registra; esperar la estabilidad lo hace fiable."""
    prev = None
    tries = 0
    t0 = time.time()
    while True:
        tries += 1
        p = cxtlib.cdp(cxt, tab, expr)
        if p and prev and p.get("x") == prev.get("x") and p.get("y") == prev.get("y"):
            return p, {"tries": tries, "hit": True, "timed_out": False}
        prev = p
        if time.time() - t0 >= timeout:
            return p, {"tries": tries, "hit": bool(p), "timed_out": not bool(p)}
        time.sleep(TICK)


def open_share(cxt, tab, expect="", stats=None):
    """Abre el menu nativo de Compartir. NUNCA pulsa Escape aqui: cerraria la
    VENTANA (modal) del post y compartiriamos el feed de fondo (post equivocado)."""
    if cxtlib.cdp(cxt, tab, JS_PICKER) or cxtlib.cdp(cxt, tab, JS_MENU):
        if stats is not None:
            stats.update({"tries": 1, "hit": True, "timed_out": False})
        return True
    for _ in range(3):
        f = _find_share_scrolling(cxt, tab, expect, timeout=4.0)
        if not f:
            continue
        if not share_point_ok(f):
            reason = f.get("reason") or "sin-punto"
            log("  Compartir no disponible: %s" % reason)
            if stats is not None:
                stats.update({"reason": reason, "scope": f.get("scope")})
            return False
        js_click(cxt, tab, f["x"], f["y"])              # click rapido (JS)
        settle(cxt, tab)
        v, info = poll_js(cxt, tab, JS_MENU_OR_PICKER, timeout=1.5, tick=TICK,
                          step="open_share")
        if not v:                                        # JS ignorado: click fiel
            cxtlib.click(cxt, tab, f["x"], f["y"])
            settle(cxt, tab)
            v, info = poll_js(cxt, tab, JS_MENU_OR_PICKER, timeout=2.5, tick=TICK,
                              step="open_share")
        if stats is not None:
            stats.update(info)
        if v:
            return True
    return False


def _wait_picker_empty(cxt, tab, timeout=1.5):
    """Espera a que el input del picker quede estable (el Reset limpia selecciones)."""
    prev = cxtlib.cdp(cxt, tab, JS_PICKER_VALUE) or ""
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cxtlib.cdp(cxt, tab, JS_PICKER_VALUE) or ""
        if v == prev:
            return True
        prev = v
        time.sleep(JS_TICK)
    return True


def open_group_option(cxt, tab, stats=None):
    """Abre SOLO el buscador de grupos. Nunca pulsa contactos ni Messenger."""
    if cxtlib.cdp(cxt, tab, JS_PICKER):
        if stats is not None:
            stats.update({"tries": 1, "hit": True, "timed_out": False})
        return True
    t0 = time.time()
    tries = 0
    while time.time() - t0 < 6.0:
        tries += 1
        if cxtlib.cdp(cxt, tab, JS_GROUP_CLICK):
            settle(cxt, tab)
            v, info = poll_js(cxt, tab, JS_PICKER, timeout=3.0, tick=TICK,
                              step="group_option")
            if stats is not None:
                stats.update(info)
            if v:
                r = cxtlib.cdp(cxt, tab, JS_RESET)   # Multi-Share persiste selecciones: limpiar
                if r:
                    js_click(cxt, tab, r["x"], r["y"])
                    _wait_picker_empty(cxt, tab, timeout=1.5)
                return True
            if stats is not None:
                stats["tries"] = tries
            print("group_option: el tile Grupo no abrio el buscador; aborto", flush=True)
            return False
        time.sleep(TICK)
    if cxtlib.cdp(cxt, tab, JS_PICKER):
        return True
    if stats is not None:
        stats.update({"tries": tries, "hit": False, "timed_out": True})
    print("group_option: no aparecio el tile Grupo", flush=True)
    return False


def type_search(cxt, tab, name, timeout=1.5):
    """Escribe el nombre y espera a que el input sea no-vacio y estable (~80 ms)."""
    cxtlib.cdp(cxt, tab, JS_CLEAR_ACTIVE)            # limpia (native setter + input event)
    cxtlib.call(cxt, "cdptype", {"tabId": tab, "text": name})
    prev = None
    t0 = time.time()
    while True:
        v = cxtlib.cdp(cxt, tab, JS_PICKER_VALUE) or ""
        if v and v == prev:
            return True
        prev = v
        if time.time() - t0 >= timeout:
            return bool(v)
        time.sleep(JS_TICK)


def _wait_submit(cxt, tab, timeout=6.0, step="submit"):
    """Espera un Publicar VALIDADO (contenedor de grupo y punto pulsable).

    Devuelve (pick|None, info). Completa transiciones en cada intento: el
    compositor de grupo entra deslizandose y en esta ventana la transicion no
    avanza sola, dejandolo tapado por el dialogo anterior."""
    tries = 0
    t0 = time.time()
    s = None
    while True:
        tries += 1
        settle(cxt, tab)
        s = cxtlib.cdp(cxt, tab, JS_SUBMIT)
        if s and s.get("x") is not None and not s.get("reason"):
            return s, {"tries": tries, "hit": True, "timed_out": False}
        if time.time() - t0 >= timeout:
            if step:
                print("poll timeout %s tras %.1fs (tries=%d) reason=%s"
                      % (step, time.time() - t0, tries,
                         (s or {}).get("reason") or "sin-boton"), flush=True)
            return s, {"tries": tries, "hit": bool(s and s.get("x") is not None),
                       "timed_out": True}
        time.sleep(TICK)


def _wait_share_context(cxt, tab, expect, post_url, timeout=6.0, step="share_ctx"):
    t0 = time.time()
    tries = 0
    last = None
    while True:
        tries += 1
        last = cxtlib.cdp(cxt, tab, js_share_context(expect, post_url))
        if classify_share_context(last)[0]:
            return last, {"tries": tries, "hit": True, "timed_out": False}
        if time.time() - t0 >= timeout:
            if step:
                print("poll timeout %s tras %.1fs (tries=%d) reason=%s"
                      % (step, time.time() - t0, tries,
                         (last or {}).get("reason") or "sin-contexto"), flush=True)
            return last, {"tries": tries, "hit": False, "timed_out": True}
        time.sleep(TICK)


def _isolate_submit(cxt, tab, stats=None):
    """Deja el boton Publicar ARRIBA: si un dialogo viejo lo cubre, cierra su X.

    Solo actua cuando JS_SUBMIT dice 'cubierto' (elementFromPoint no es el
    boton). Nunca usa Escape (cerraria el compositor). Devuelve el pick final:
    si sigue cubierto, submit aborta en vez de pulsar a ciegas."""
    closed = 0
    s = cxtlib.cdp(cxt, tab, JS_SUBMIT)
    t0 = time.time()
    while (isinstance(s, dict) and s.get("reason") == "cubierto"
           and closed < 4 and time.time() - t0 < 6.0):
        c = cxtlib.cdp(cxt, tab, JS_STALE_CLOSE)
        if not (isinstance(c, dict) and c.get("x") is not None):
            break
        cxtlib.click(cxt, tab, c["x"], c["y"])
        closed += 1
        settle(cxt, tab)
        time.sleep(0.2)
        s = cxtlib.cdp(cxt, tab, JS_SUBMIT)
    if stats is not None:
        stats["dialogs_closed"] = closed
        stats["dialogs"] = _dialog_count(cxt, tab)
    if closed:
        log("   cerre %d dialogo(s) que tapaban Publicar" % closed)
    return s


def search_and_select(cxt, tab, name, stats=None, expect="", post_url="", dry=True):
    """Selecciona el grupo; en simulacro no pulsa ninguna accion final."""
    if not poll_js(cxt, tab, JS_PICKER, timeout=3.0, tick=TICK, step="search_picker")[0]:  # JS_PICKER hace scroll+focus
        return False
    if not type_search(cxt, tab, name):
        poll_js(cxt, tab, JS_PICKER, timeout=1.2, tick=TICK, step="search_picker2")
        type_search(cxt, tab, name)
    sel, sinfo = poll_js(cxt, tab, js_select_group(name), timeout=3.0, tick=TICK,
                         step="select_group")
    if stats is not None:
        stats.update(sinfo)
        stats["hit"] = bool(sel)
    if not sel:
        return False
    settle(cxt, tab)
    time.sleep(0.4)
    if dry:
        log("   simulacro: grupo seleccionado; no se pulsa Compartir ahora")
        if stats is not None:
            stats["dry_stop"] = True
        return True
    act = None
    t0 = time.time()
    while time.time() - t0 < 2.5:
        act = cxtlib.cdp(cxt, tab, JS_PICKER_SHARE)
        if isinstance(act, dict) and act.get("x") is not None and not act.get("disabled"):
            break
        time.sleep(TICK)
    if isinstance(act, dict) and act.get("x") is not None and not act.get("disabled"):
        js_click(cxt, tab, act["x"], act["y"])           # "Compartir ahora"
        settle(cxt, tab)
        s, info = _wait_submit(cxt, tab, timeout=4.0, step="search_share")
        if not (s and s.get("x") is not None and not s.get("reason")):
            # Reintento SOLO si "Compartir ahora" sigue en el MISMO sitio: tras
            # el primer click el picker puede haberse cerrado y ese punto ya es
            # otra cosa (p. ej. Publicar). Nunca click a ciegas.
            act2 = cxtlib.cdp(cxt, tab, JS_PICKER_SHARE)
            if (isinstance(act2, dict) and act2.get("x") is not None
                    and not act2.get("disabled")
                    and abs(act2["x"] - act["x"]) <= 2
                    and abs(act2["y"] - act["y"]) <= 2):
                js_click(cxt, tab, act2["x"], act2["y"])
                settle(cxt, tab)
                s, info = _wait_submit(cxt, tab, timeout=4.0, step="search_share")
    else:
        s, info = _wait_submit(cxt, tab, timeout=4.0, step="search_submit")
    if stats is not None:
        stats.update(info)
    if not (s and s.get("x") is not None and not s.get("reason")):
        return False
    ev, cinfo = _wait_share_context(cxt, tab, expect, post_url, timeout=6.0,
                                    step="search_share_ctx")
    ok, why = classify_share_context(ev)
    if stats is not None:
        stats.update(cinfo)
        stats["share_ctx"] = ev
        stats["share_reason"] = why
    if not ok:
        log("   ABORTO (%s): el compositor no contiene el post original" % why)
        return False
    return True


def classify_send_verdict(proof="", weak="", pending_hint="", clicked=True, verdict_reason=""):
    """Veredicto PURO del envio, sin navegador: devuelve (ok, reason, pending).

    Orden: una prueba fuerte gana siempre. Si no hay prueba pero FB aviso que la
    publicacion quedo pendiente de aprobacion -> NO es exito (quiza nunca se
    publique) ni fallo del motor: `pendiente-aprobacion`. Si solo hay pista debil
    (compositor cerrado / aviso de publicacion) -> `publicado-sin-confirmar`.
    Sin nada -> el motivo real (`sin-prueba`, `dialog-abierto`, ...)."""
    if proof:
        return True, proof, False
    if pending_hint and clicked:
        return False, "pendiente-aprobacion", True
    if weak:
        return False, "publicado-sin-confirmar", False
    return False, (verdict_reason or "sin-prueba"), False


def submit(cxt, tab, dry, text="", stats=None, verify=None,
           share_required=False, share_expect="", share_post_url=""):
    """Publica SOLO si el compositor activo es de grupo y el boton es suyo.

    ok=True exige PRUEBA fuerte: permalink NUEVO del post, o una copia MAS que
    la linea base en el feed del grupo. El cierre del compositor y el aviso de
    publicacion son solo PISTAS, no pruebas. Si FB avisa que la publicacion
    quedo PENDIENTE de aprobacion se reporta `pendiente-aprobacion` (ni exito ni
    fallo del motor). Cualquier duda -> ok=False con motivo
    (`publicado-sin-confirmar` si hubo pista pero ninguna prueba).
    `verify` trae {fbid, gid, before, needle, author, group}: sin gid no hay
    permalink que buscar y se conserva la pista debil, marcada como tal."""
    s, info = _wait_submit(cxt, tab, timeout=6.0, step="submit")
    if stats is not None:
        stats.update(info)
    if not s:
        return {"ok": False, "stage": "submit-not-found", "reason": "sin boton publicar"}
    why = s.get("reason")
    if why:
        log("   ABORTO (%s): no pulso Publicar" % why)
        return {"ok": False, "stage": why, "reason": why, "label": s.get("label"),
                "audience": s.get("audience") or ""}
    if share_required:
        ev, sinfo = _wait_share_context(cxt, tab, share_expect, share_post_url,
                                        timeout=6.0, step="submit_share_ctx")
        ok, scwhy = classify_share_context(ev)
        if stats is not None:
            stats.update(sinfo)
            stats["share_ctx"] = ev
            stats["share_reason"] = scwhy
        if not ok:
            log("   ABORTO (%s): el compositor no contiene el post original" % scwhy)
            close_share_dialog(cxt, tab)
            return {"ok": False, "stage": scwhy, "reason": scwhy,
                    "label": s.get("label"), "audience": s.get("audience") or ""}
    if text:
        cxtlib.call(cxt, "cdptype", {"tabId": tab, "text": text})
        s, _ = _wait_submit(cxt, tab, timeout=2.0, step="submit_text")
        if not s or s.get("reason"):
            return {"ok": False, "stage": "submit-text", "reason": s.get("reason") if s else "sin-boton"}
    if dry:
        if stats is not None:
            stats["dry"] = True
            stats["proof"] = "dry"
        return {"ok": True, "dry": True, "label": s.get("label"), "x": s["x"], "y": s["y"],
                "reason": "dry"}
    s = _isolate_submit(cxt, tab, stats)
    if not s or s.get("reason"):
        why = (s or {}).get("reason") or "sin-boton"
        log("   ABORTO tras aislar (%s): no pulso Publicar" % why)
        return {"ok": False, "stage": why, "reason": why, "label": (s or {}).get("label"),
                "audience": (s or {}).get("audience") or ""}
    top = cxtlib.cdp(cxt, tab, JS_POINT_STACK % (s["x"], s["y"]))
    print("   punto %d,%d encima: %s" % (s["x"], s["y"], json.dumps(top, ensure_ascii=False)),
          flush=True)
    cxtlib.cdp(cxt, tab, JS_ARM_HIT)
    pending_pre = cxtlib.cdp(cxt, tab, JS_PENDING_NOTICE) or ""
    before_pids = _visible_pids(cxt, tab)  # linea base ANTES del click
    if stats is not None:
        stats["before_pids"] = len(before_pids)
    click_ts = int(time.time())
    cxtlib.click(cxt, tab, s["x"], s["y"])              # click FIEL para el paso que publica
    hit = cxtlib.cdp(cxt, tab, JS_HIT)
    click_hit = bool(isinstance(hit, dict)
                     and abs(int(hit.get("x", -99)) - s["x"]) <= 2
                     and abs(int(hit.get("y", -99)) - s["y"]) <= 2)
    if stats is not None:
        stats["click_hit"] = click_hit
    log("   click en %d,%d entregado=%s (%s)"
        % (s["x"], s["y"], click_hit, json.dumps(hit, ensure_ascii=False)))
    # PISTA debil: el compositor se cerro o hay aviso de publicacion. NO es prueba:
    # el compositor puede cerrarse sin que el post exista. Prueba fuerte = permalink
    # del post nuevo, o una copia MAS que la linea base en el feed del grupo.
    weak = ""
    signal = ""
    closed = poll_js_settle(cxt, tab, JS_COMPOSER_CLOSED, timeout=9.0, tick=0.15,
                            step="submit_close")[0]
    if closed:
        weak = "composer-cerrado"
    else:
        signal = cxtlib.cdp(cxt, tab, JS_PUBLISH_SIGNAL)
        if signal:
            log("   pista de publicacion observada: %s" % signal)
            weak = "confirmado"
    pending_hint = pending_pre or (cxtlib.cdp(cxt, tab, JS_PENDING_NOTICE) or "")
    if not pending_hint and signal and "pendient" in signal.lower():
        pending_hint = signal
    if pending_hint:
        log("   aviso de aprobacion pendiente: %s" % pending_hint)
    proof = ""
    verdict_reason = ""
    if verify and verify.get("gid"):
        # 1) permalink del post nuevo: la prueba directa.
        needle = verify.get("needle") or text
        cp = confirm_new_post_permalink(
            cxt, tab, verify.get("gid", ""), needle=needle,
            fbid=verify.get("fbid", ""), group=verify.get("group", ""),
            not_before_ts=click_ts - 300, before_pids=before_pids)
        if stats is not None:
            stats["permalink"] = cp
        log("   permalink nuevo: %s (%s)" % (cp.get("url") or "-", cp.get("reason")))
        if cp.get("ok"):
            proof = "permalink-nuevo"
        else:
            verdict_reason = cp.get("reason") or "sin-prueba"
            if fastmode.enabled():
                # FAST: el feed es caro y fragil; sin permalink no hay prueba.
                # La pista debil se conserva como senal, nunca como prueba.
                log("   modo fast: se omite la verificacion de feed")
            else:
                # 2) feed del grupo: una copia MAS que la linea base.
                v = verify_feed(cxt, tab, verify.get("gid", ""), verify.get("fbid", ""),
                                before=verify.get("before"), needle=needle,
                                author=verify.get("author", ""),
                                group=verify.get("group", ""),
                                shot_path=os.environ.get("CXT_FEED_SHOT") or "")
                verify["after"] = v.get("count")
                verify["verdict"] = v
                if stats is not None:
                    stats["feed"] = v
                log("   feed del grupo: antes=%s despues=%s (%s)"
                    % (verify.get("before"), v.get("count"), v.get("reason")))
                if v.get("ok"):
                    proof = "feed"
                    verdict_reason = ""
                else:
                    verdict_reason = v.get("reason") or verdict_reason
    elif weak and not pending_hint:
        # Sin gid no hay permalink que buscar. La pista debil NUNCA concede
        # ok=True: se reporta `publicado-sin-confirmar` (ni exito ni fallo).
        if stats is not None:
            stats["proofless"] = True
        log("   AVISO: sin gid, no puedo confirmar por permalink (pista debil: %s)" % weak)
    if stats is not None:
        stats["proof"] = proof
        stats["signal"] = weak
        stats["pending_hint"] = pending_hint
    ok, reason, pending = classify_send_verdict(
        proof=proof, weak=weak, pending_hint=pending_hint, clicked=click_hit,
        verdict_reason=verdict_reason or ("dialog-abierto" if not closed else "sin-prueba"))
    if stats is not None:
        stats["pending"] = pending
    if ok:
        return {"ok": True, "published": True, "label": s.get("label"),
                "audience": s.get("audience") or "", "reason": reason}
    if pending:
        log("   AVISO: FB dejo la publicacion pendiente de aprobacion (sin copia confirmada)")
    else:
        log("   SIN PRUEBA de publicacion: ni permalink ni feed la confirman")
    print("   submit tras click: %s" % json.dumps(cxtlib.cdp(cxt, tab, JS_SUBMIT), ensure_ascii=False),
          flush=True)
    print("   stack tras click : %s" % json.dumps(cxtlib.cdp(cxt, tab, JS_STACK), ensure_ascii=False),
          flush=True)
    return {"ok": False, "published": False, "stage": reason, "reason": reason,
            "pending": pending,
            "label": s.get("label"), "audience": s.get("audience") or ""}


def _stack(cxt, tab):
    st = cxtlib.cdp(cxt, tab, JS_STACK)
    return st if isinstance(st, dict) else {"any": False}


def _dialog_count(cxt, tab):
    v = cxtlib.cdp(cxt, tab, JS_DIALOG_COUNT)
    return int(v) if isinstance(v, (int, float)) else -1


def close_all_dialogs(cxt, tab, timeout=10.0, allow_reload=True, stats=None):
    """Cierra TODOS los dialogos visibles y AFIRMA el resultado con un conteo.

    Antes devolvia True si no habia "superficies conocidas" (JS_STACK), aunque
    quedaran dialogos abiertos: 206 ms diciendo limpio con la pila viva. Ahora
    cuenta div[role=dialog] visibles antes/despues y solo devuelve True si el
    conteo final es 0; si un X no es pulsable recarga y vuelve a contar."""
    t0 = time.time()
    before = _dialog_count(cxt, tab)
    tries = 0
    n = before
    while n > 0 and time.time() - t0 < timeout:
        tries += 1
        settle(cxt, tab)
        x = cxtlib.cdp(cxt, tab, JS_TOP_CLOSE)
        if isinstance(x, dict) and x.get("x") is not None:
            cxtlib.click(cxt, tab, x["x"], x["y"])
            time.sleep(0.15)
            n = _dialog_count(cxt, tab)
            continue
        cxtlib.call(cxt, "cdpkey", {"tabId": tab, "key": "Escape"})
        cxtlib.call(cxt, "cdp", {"tabId": tab, "method": "Page.handleJavaScriptDialog",
                                 "params": {"accept": True}})
        d = cxtlib.cdp(cxt, tab, JS_DISCARD)
        if isinstance(d, dict) and d.get("x") is not None:
            cxtlib.click(cxt, tab, d["x"], d["y"])
        time.sleep(0.35)
        n = _dialog_count(cxt, tab)
    if n and allow_reload:
        cur = cxtlib.cdp(cxt, tab, "location.href") or ""
        if cur:
            log("   pila tapada (%s dialogos): recargo la pestana para partir limpio" % n)
            cxtlib.call(cxt, "navigate", {"tabId": tab, "url": cur, "timeout": 30000}, 90)
            poll_js(cxt, tab, JS_READY, timeout=10.0, tick=TICK)
            poll_js(cxt, tab, "(" + JS_DIALOG_COUNT + ")===0", timeout=8.0, tick=TICK,
                    step="cleanup_reload")
            n = _dialog_count(cxt, tab)
    after = _dialog_count(cxt, tab)
    ok = after == 0 and before >= 0
    if stats is not None:
        stats.update({"tries": tries, "hit": ok, "timed_out": not ok,
                      "dialogs_before": before, "dialogs_after": after})
    if before != 0 or after != 0:
        log("   cleanup dialogos: %s -> %s (%s)" % (before, after, "ok" if ok else "FALLO"))
    return ok


def close_share_dialog(cxt, tab):
    """Cierra el menu/picker/compositor de compartir; NO la ventana del post."""
    n = None
    for _ in range(8):
        n = cxtlib.cdp(cxt, tab, JS_NONPOST_COUNT)
        if isinstance(n, int) and n <= 0:
            return True
        x = cxtlib.cdp(cxt, tab, JS_TOP_NONPOST_CLOSE)
        if not (isinstance(x, dict) and x.get("x") is not None):
            break
        cxtlib.click(cxt, tab, x["x"], x["y"])
        settle(cxt, tab)
        # Al cerrar el compositor Facebook puede pedir "Descartar publicacion".
        # Es una capa no-post; confirmarla evita que el loop se quede atascado.
        d = cxtlib.cdp(cxt, tab, JS_DISCARD)
        if isinstance(d, dict) and d.get("x") is not None:
            cxtlib.click(cxt, tab, d["x"], d["y"])
            settle(cxt, tab)
    n = cxtlib.cdp(cxt, tab, JS_NONPOST_COUNT)
    if isinstance(n, int) and n <= 0:
        return True
    # Si las capas no-post no se dejan cerrar, recargamos la publicacion actual.
    # Es un reset seguro: nunca pulsa el Compartir del feed de fondo.
    cur = cxtlib.cdp(cxt, tab, "location.href") or ""
    if re.search(r"/posts/|/permalink|/share/p/|/photo|/videos/", cur):
        cxtlib.call(cxt, "navigate", {"tabId": tab, "url": cur, "timeout": 30000})
        time.sleep(1.5)
        n = cxtlib.cdp(cxt, tab, JS_NONPOST_COUNT)
        return isinstance(n, int) and n <= 0
    return False


def goto_post(cxt, tab, url):
    cxtlib.call(cxt, "cdp", {"tabId": tab, "method": "Page.handleJavaScriptDialog",
                             "params": {"accept": True}})
    cxtlib.clear_draft(cxt, tab)
    cur = cxtlib.cdp(cxt, tab, "location.href") or ""
    # No recargar si ya estamos en ESA publicacion: el navigate de Facebook
    # puede tardar ~20 s y no aporta nada (el post ya esta cargado). La
    # identidad la confirma _identity con el boton Compartir presente.
    if not (url and cur.split("#")[0] == url.split("#")[0]):
        cxtlib.call(cxt, "navigate", {"tabId": tab, "url": url, "timeout": 20000})
    cxtlib.call(cxt, "cdp", {"tabId": tab, "method": "Page.handleJavaScriptDialog",
                             "params": {"accept": True}})
    poll_js(cxt, tab, JS_READY, timeout=1.0, tick=TICK)   # settle por condicion


def is_boilerplate_expect(expect):
    n = cxtlib.norm(expect or "")
    if not n:
        return True
    words = [w for w in n.split() if w]
    noise = {"facebook", "meta", "instagram", "whatsapp", "me", "gusta", "like",
             "comentar", "comment", "compartir", "share", "ver", "mas", "see",
             "more", "publicidad", "advertisement"}
    return bool(words) and all(w in noise or w.isdigit() for w in words)


def context_ok(cxt, tab, expect):
    if not expect or is_boilerplate_expect(expect):
        return True
    t = cxtlib.cdp(cxt, tab, JS_POST_TEXT) or ""
    return cxtlib.norm(expect) in cxtlib.norm(t)


def _identity(cxt, tab, url, expect):
    """Confirma que la publicacion a compartir es la correcta. -> (expect, error|None)."""
    exp = "" if is_boilerplate_expect(expect) else (expect or "")
    if url:
        log("abro la publicacion: %s" % url)
        goto_post(cxt, tab, url)
        ok = poll_js(cxt, tab, js_find_share(exp), timeout=5.0, tick=TICK)[0]
        if not share_point_ok(ok):
            if isinstance(ok, dict) and ok.get("reason"):
                log("no puedo confirmar el post: %s" % ok["reason"])
            cur = cxtlib.cdp(cxt, tab, "location.href") or ""
            base = cur.split("#")[0].split("?")[0]
            log("aun no veo el post; reintento por permalink: %s" % (base or url))
            goto_post(cxt, tab, base or url)
            ok = poll_js(cxt, tab, js_find_share(exp), timeout=5.0, tick=TICK)[0]
        if not share_point_ok(ok):
            log("ERROR: no pude confirmar la publicacion (identity)")
            return exp, {"ok": False, "stage": "identity", "reason": "identity"}
    elif not context_ok(cxt, tab, expect):
        log("ERROR: el post visible no coincide con '%s' (identity)" % expect)
        return exp, {"ok": False, "stage": "identity", "reason": "identity"}
    return exp, None


def _photo_fbid(url):
    m = re.search(r"[?&]fbid=(\d+)", url or "")
    return m.group(1) if m else ""


def is_photo_viewer_url(url):
    """True si la URL es el visor de una foto, no el permalink del post.

    Compartir desde el visor toma la FOTO (y su atribucion), no el post del
    enlace; por eso hay que resolverlo antes de abrir el menu de compartir.
    """
    u = (url or "").lower()
    return "/photo/?" in u or "/photo?" in u


def _clean_canonical(u):
    """Quita tracking (?__tn__=*F) sin perder los ids de permalink.php."""
    u = (u or "").strip()
    if not u:
        return ""
    if "permalink.php" in u:
        story = re.search(r"[?&]story_fbid=(\d+)", u)
        owner = re.search(r"[?&]id=(\d+)", u)
        if story:
            q = "story_fbid=" + story.group(1)
            if owner:
                q += "&id=" + owner.group(1)
            return u.split("?")[0] + "?" + q
    base = u.split("#")[0].split("?")[0]
    if base.endswith("/") and base.count("/") > 3:
        base = base.rstrip("/")
    return base


def _is_post_level_url(u):
    u = (u or "").lower()
    if not u or "/photo/" in u or "/reel/" in u or "/watch/" in u:
        return False
    return ("/posts/" in u or "/permalink" in u or "story_fbid=" in u
            or ("/groups/" in u and "/posts/" in u))


def pick_canonical_post_url(url, links):
    """Elige el permalink del post desde los enlaces del visor de foto.

    Prefiere el post de grupo cuyo id coincide con `set=pcb.<id>`; si no, el
    primer enlace post-level del mismo host. Devuelve None si no hay ninguno.
    """
    if not is_photo_viewer_url(url):
        return _clean_canonical(url)
    cands = []
    for raw in links or []:
        if not isinstance(raw, str) or not _is_post_level_url(raw):
            continue
        cu = _clean_canonical(raw)
        if cu and cu not in cands:
            cands.append(cu)
    if not cands:
        return None
    m = re.search(r"[?&]set=pcb\.(\d+)", url or "")
    pcb = m.group(1) if m else ""
    if pcb:
        for cu in cands:
            if re.search(r"/posts/" + re.escape(pcb) + r"/?(?:$|[?#])", cu):
                return cu
    host = ""
    try:
        host = (url or "").split("/")[2].lower()
    except IndexError:
        host = ""
    for cu in cands:
        if host and host in cu.lower():
            return cu
    return cands[0]


JS_POST_LINKS = (
    "(()=>{const bad=(h)=>/\\/photo\\/|\\/reel\\/|\\/watch\\//.test(h);"
    "const good=(h)=>/\\/posts\\//.test(h)||/\\/permalink/.test(h)||"
    "/[?&]story_fbid=/.test(h)||(/\\/groups\\//.test(h)&&/\\/posts\\//.test(h));"
    "const out=[];for(const a of document.querySelectorAll('a[href]')){"
    "const h=a.href||'';if(good(h)&&!bad(h)&&out.indexOf(h)<0)out.push(h);}"
    "return out;})()"
)


def resolve_photo_viewer_url(cxt, tab, url):
    """Resuelve un visor `photo/?fbid=...` al permalink canonico del post.

    - URL normal: se devuelve tal cual.
    - Visor con permalink: navega al visor, lee los enlaces, navega al post.
    - Visor sin permalink: devuelve "" (fail-closed: no compartir a ciegas).
    """
    if not url or not is_photo_viewer_url(url):
        return url or ""
    goto_post(cxt, tab, url)
    links = cxtlib.cdp(cxt, tab, JS_POST_LINKS) or []
    canon = pick_canonical_post_url(url, links)
    if not canon:
        log("   ERROR: visor de foto sin permalink de post; no comparto a ciegas")
        return ""
    if canon.split("#")[0].split("?")[0] != url.split("#")[0].split("?")[0]:
        log("   visor de foto -> post: %s" % canon)
        goto_post(cxt, tab, canon)
    return canon


def _group_id(group):
    """Id del grupo: si ya es numerico se usa; si es nombre, groups.json."""
    g = (group or "").strip()
    if g.isdigit():
        return g
    tgt = cxtlib.norm(g)
    groups = cxtlib.load_json(os.path.join(cxtlib.resolve_dir(), "groups.json")).get("groups", [])
    exact = [str(it.get("id")) for it in groups if cxtlib.norm(it.get("name") or "") == tgt]
    if exact:
        return exact[0]
    partial = [str(it.get("id")) for it in groups if tgt and tgt in cxtlib.norm(it.get("name") or "")]
    return partial[0] if len(partial) == 1 else ""


def js_feed_count(fbid="", needle=""):
    n = cxtlib.norm(needle or "")
    return ("(()=>{const feed=document.querySelector('div[role=feed]')||document.body;"
            "const kids=[...feed.querySelectorAll(':scope>div')];"
            "const fbid=" + json.dumps(fbid or "") + ";"
            "const need=" + json.dumps(n) + ";"
            "const hit=(el)=>{if(fbid)return !!el.querySelector('a[href*=\"fbid='+fbid+'\"]');"
            "return (el.textContent||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'')"
            ".toLowerCase().includes(need);};"
            "if(kids.length)return kids.filter(hit).length;"
            "return fbid?document.querySelectorAll('a[href*=\"fbid='+fbid+'\"]').length:0;})()")


JS_FEED_STATE = ("(()=>{const b=(document.body&&document.body.innerText)||'';"
                 "return {ready:document.readyState,body_len:b.length,"
                 "feed:!!document.querySelector('div[role=feed]'),"
                 "articles:document.querySelectorAll('div[role=article]').length};})()")

# Nombre del usuario logueado (el autor de la copia). Sale del bloque que FB
# incrusta en el HTML; no depende de que el perfil este visible.
JS_ME_NAME = r"""(()=>{try{const h=document.documentElement.innerHTML||'';
const m=h.match(/"CurrentUserInitialData".{0,500}?"NAME":"((?:[^"\\]|\\.)+)"/);
return m?JSON.parse('"'+m[1]+'"'):'';}catch(e){return '';}})()"""

# Id del usuario logueado, de los datos iniciales que FB incrusta en la pagina.
# No se hardcodea: si no esta, el fallback por permalink falla cerrado.
JS_ME_UID = r"""(()=>{try{const h=document.documentElement.innerHTML||'';
let m=h.match(/"CurrentUserInitialData".{0,1200}?"USER_ID":"(\d+)"/);
if(!m)m=h.match(/"userID":"(\d+)"/);
return m?m[1]:'';}catch(e){return '';}})()"""


def js_feed_probe(gid, fbid="", needle="", author="", group=""):
    """Sonda del FEED: reporta cada senal por separado y un conteo conservador.

    No suma senales: `count` usa las copias identificadas por el fbid de la
    foto (una por contenedor/post), o las apariciones del `needle` si no hay
    fbid. `author_hits`/`group_hits`/`fbid_html` son corroboracion, no posts.
    """
    tpl = r"""(()=>{
const gid=__GID__,fbid=__FBID__,needle=__NEEDLE__,author=__AUTHOR__,group=__GROUP__;
const norm=(s)=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[\u200b-\u200f\u2060\ufeff\u00ad]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
const countOcc=(hay,n)=>{if(!n)return 0;let i=-1,c=0;while((i=hay.indexOf(n,i+1))>=0)c++;return c;};
const body=(document.body&&document.body.innerText)||'';
const html=(document.documentElement&&document.documentElement.innerHTML)||'';
const nb=norm(body),nh=norm(html);
const feed=document.querySelector('div[role=feed]');
const feedText=norm(feed?feed.innerText:'');
const hay=(feedText.length>200)?feedText:nb;
const arts=[...document.querySelectorAll('div[role=article]')];
const artIdx=new Map();arts.forEach((e,i)=>artIdx.set(e,i));
const fbidAnchors=fbid?[...document.querySelectorAll('a[href*="fbid='+fbid+'"]')]:[];
const seen=new Set();let copies=0;
for(const a of fbidAnchors){
  let n=a,key='';
  for(let i=0;n&&i<16;i++,n=n.parentElement){
    if(n.getAttribute&&n.getAttribute('role')==='article'){key='art:'+(artIdx.has(n)?artIdx.get(n):('x'+i));break;}
    const links=n.querySelectorAll?n.querySelectorAll('a[href*="/groups/'+gid+'/posts/"],a[href*="/groups/'+gid+'/permalink/"]'):[];
    for(const l of links){const m=(l.getAttribute('href')||'').match(/(?:posts|permalink)\/(\d+)/);if(m){key='post:'+m[1];break;}}
    if(key)break;
  }
  if(!key)key='anchor';
  if(!seen.has(key)){seen.add(key);copies++;}
}
const postLinks=[...document.querySelectorAll('a[href]')].map(a=>a.getAttribute('href')||'')
  .filter(h=>new RegExp('/groups/'+gid+'/(?:posts|permalink)/\\d+').test(h)
          &&!/[?&](comment_id|reply_comment_id|notif_id|notif_t)=/.test(h));
const postIds=[...new Set(postLinks.map(h=>((h.match(/(?:posts|permalink)\/(\d+)/)||[])[1])).filter(Boolean))];
const needleHits=needle?countOcc(hay,needle):0;
const comoHits=author?countOcc(hay,'comentar como '+author):0;
const authorHits=author?Math.max(0,countOcc(hay,author)-comoHits):0;
const groupHits=group?countOcc(nb,group):0;
const fbidHtml=fbid?countOcc(nh,'fbid='+fbid):0;
const count=fbid?copies:needleHits;
let snippet='';
if(fbidAnchors.length){let r=fbidAnchors[0];for(let i=0;i<9&&r&&r.parentElement;i++)r=r.parentElement;snippet=norm(r.innerText||'').slice(0,260);}
if(!snippet&&needle){const i=nb.indexOf(needle);if(i>=0)snippet=nb.slice(Math.max(0,i-120),i+160);}
if(!snippet&&authorHits){const i=nb.indexOf(author);if(i>=0)snippet=nb.slice(Math.max(0,i-120),i+160);}
if(!snippet)snippet=nb.slice(0,220);
return {count:count,fbid_copies:copies,fbid_links:fbidAnchors.length,fbid_html:fbidHtml,
        needle_hits:needleHits,author_hits:authorHits,group_hits:groupHits,
        post_links:postLinks.length,post_ids:postIds.length,post_ids_sample:postIds.slice(0,5),
        body_len:body.length,html_len:html.length,feed:!!feed,feed_len:feedText.length,
        articles:arts.length,readable:!!feed&&body.length>=800,snippet:snippet};})()"""
    return (tpl.replace("__GID__", json.dumps(str(gid or "")))
               .replace("__FBID__", json.dumps(str(fbid or "")))
               .replace("__NEEDLE__", json.dumps(cxtlib.norm(needle or "")))
               .replace("__AUTHOR__", json.dumps(cxtlib.norm(author or "")))
               .replace("__GROUP__", json.dumps(cxtlib.norm(group or ""))))


def _me_name(cxt, tab):
    """Nombre del usuario logueado, para la senal de autor. '' si no se pudo."""
    v = cxtlib.cdp(cxt, tab, JS_ME_NAME)
    return v if isinstance(v, str) else ""


def _me_uid(cxt, tab):
    """Id del usuario logueado ('' si la pagina no lo expone)."""
    v = cxtlib.cdp(cxt, tab, JS_ME_UID)
    return v if isinstance(v, str) and v.isdigit() else ""


# Candidatos de post desde el HTML de la pagina del miembro dentro del grupo.
# Facebook NO pinta <a href> a los posts propios en esa vista: los datos viven
# en el JSON incrustado (post_id, creation_time, wwwURL, target_group, foto).
# Tambien se recogen anclas reales por si la vista (p. ej. busqueda) si las pinta.
JS_POST_CANDIDATES = r"""(()=>{
const gid=__GID__;
const h=(document.documentElement&&document.documentElement.innerHTML)||'';
const unesc=(s)=>{try{return JSON.parse('"'+s+'"');}catch(e){return s;}};
const clean=(s)=>(s||'').replace(/\s+/g,' ').trim();
// Epoch en milisegundos. Acepta numeros (segundos o ms) e ISO 8601.
const toEpochMs=(v)=>{
  if(v===null||v===undefined)return 0;
  const s=String(v).trim();
  if(!s)return 0;
  if(/^\d+(?:\.\d+)?$/.test(s)){
    let n=parseFloat(s);
    if(!isFinite(n)||n<=0)return 0;
    if(n<1e12)n=n*1000;  // segundos -> ms
    return Math.floor(n);
  }
  const ms=Date.parse(s);
  return isFinite(ms)&&ms>0?ms:0;
};
// data-utime en la propia ancla, sus ancestros cercanos o su contenedor.
const tsFromAnchor=(a)=>{
  const ua=(n)=>{const u=n&&n.getAttribute&&n.getAttribute('data-utime');return u?toEpochMs(u):0;};
  let n=a,hops=0;
  while(n&&hops++<4){const t=ua(n);if(t)return t;n=n.parentElement;}
  const cont=a.closest('div[role=article]')||a.parentElement;
  if(cont){
    const du=cont.querySelector('[data-utime]');
    if(du){const t=toEpochMs(du.getAttribute('data-utime'));if(t)return t;}
    const tm=cont.querySelector('abbr[title], time[datetime]');
    if(tm){const t=toEpochMs(tm.getAttribute('title')||tm.getAttribute('datetime'));if(t)return t;}
  }
  const dn=a.querySelector('[data-utime], abbr[title], time[datetime]');
  if(dn)return toEpochMs(dn.getAttribute('data-utime')||dn.getAttribute('title')||dn.getAttribute('datetime'));
  return 0;  // sin fuente fiable: se queda en 0 (no se inventa)
};
const byPid=new Map();
const put=(pid,x)=>{
  const c=byPid.get(pid)||{pid:pid,url:'',gid:'',ts:0,text:'',fbid:'',status:'',author:''};
  if(x.url&&!c.url)c.url=x.url;
  if(x.gid&&!c.gid)c.gid=x.gid;
  if(x.ts&&!c.ts)c.ts=x.ts;
  if(x.text&&c.text.length<6000)c.text=clean(c.text+' '+x.text).slice(0,7000);
  if(x.fbid&&!c.fbid)c.fbid=x.fbid;
  if(x.status&&!c.status)c.status=x.status;
  if(x.author&&!c.author)c.author=x.author;
  byPid.set(pid,c);
};
const re=/"post_id":"(\d+)"/g;let m;let guard=0;
while((m=re.exec(h))&&guard++<500){
  const pid=m[1],s=h.slice(m.index,m.index+16000);
  const u=s.match(/"wwwURL":"(https:\\\/\\\/www\.facebook\.com\\\/groups\\\/(\d+)\\\/permalink\\\/(\d+)\\\/)"/)
        ||s.match(/"permalink_url":"(https:\\\/\\\/www\.facebook\.com\\\/groups\\\/(\d+)\\\/(?:posts|permalink)\\\/(\d+)\\\/)"/);
  const t=s.match(/"creation_time":(\d+)/);
  const st=s.match(/"unpublished_content_type":"([A-Z_]+)"/);
  const f=s.match(/\\"photo_id\\":\\"(\d+)\\"/)||s.match(/"fbid=(\d+)"/);
  const a=s.match(/"owning_profile":\{[^{}]*?"name":"((?:[^"\\]|\\.)*)"/);
  const texts=[];let x,g2=0;
  const tr=/"message":\{"text":"((?:[^"\\]|\\.)*)"/g;
  while((x=tr.exec(s))&&g2++<4)texts.push(unesc(x[1]));
  if(!texts.length){
    const tr2=/"text":"((?:[^"\\]|\\.)*)"/g;let y;let g3=0;
    while((y=tr2.exec(s))&&g3++<8)texts.push(unesc(y[1]));
  }
  put(pid,{url:u?u[1].replace(/\\\//g,'/'):'',gid:u?u[2]:'',
           ts:t?parseInt(t[1],10):0,text:texts.join(' '),
           fbid:f?f[1]:'',status:st?st[1]:'',author:a?unesc(a[1]):''});
}
for(const a of document.querySelectorAll('a[href]')){
  const href=a.getAttribute('href')||'';
  const mm=href.match(/\/groups\/(\d+)\/(?:posts|permalink)\/(\d+)/);
  if(!mm)continue;
  if(/[?&](comment_id|reply_comment_id|notif_id|notif_t)=/.test(href))continue;
  const cont=a.closest('div[role=article]');
  put(mm[2],{url:href.indexOf('http')===0?href:('https://www.facebook.com'+href),
             gid:mm[1],ts:tsFromAnchor(a),text:cont?clean(cont.innerText||''):'',
             fbid:(href.match(/[?&]fbid=(\d+)/)||[])[1]||''});
}
return [...byPid.values()];
})()"""


# Pids de posts YA visibles en la pagina, antes de pulsar Publicar. Sirve de
# linea base: un candidato con timestamp desconocido (ts=0) solo se acepta si
# su pid NO estaba aqui. Si el JS falla se devuelve un set vacio (sin baseline
# real no se relaja la puerta, ver pick_new_post_permalink).
JS_VISIBLE_PIDS = r"""(()=>{
 const out=new Set();
 const re1=/\/groups\/\d+\/(?:posts|permalink)\/(\d+)/;
 const re2=/\/permalink\/(\d+)/;
 const re3=/[?&](?:story_fbid|fbid)=(\d+)/;
 for(const a of document.querySelectorAll('a[href]')){
   const href=a.getAttribute('href')||'';
   let m=href.match(re1)||href.match(re2)||href.match(re3);
   if(m&&/^\d+$/.test(m[1]))out.add(m[1]);
 }
 return [...out];
})()"""


def _visible_pids(cxt, tab):
    """Set de pids visibles ahora; set() si el puente no responde.

    `cxtlib.cdp` ya degrada a None ante timeout/fallo; el except es solo por si
    el puente lanza algo inesperado y no debe tumbar el envio."""
    try:
        raw = cxtlib.cdp(cxt, tab, JS_VISIBLE_PIDS, timeout=10)
    except (OSError, TypeError, ValueError) as e:  # fallo del puente, no del flujo
        log("   baseline de pids no disponible: %s" % e)
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(p) for p in raw if str(p).isdigit()}


def js_post_candidates(gid):
    """JS de extraccion de candidatos, acotado al grupo `gid`."""
    return JS_POST_CANDIDATES.replace("__GID__", json.dumps(str(gid or "")))


def pick_new_post_permalink(candidates, gid, needle="", not_before_ts=None, fbid="",
                            before_pids=None):
    """Elige el mejor post NUEVO por permalink, sin navegador (funcion pura).

    Filtra gid, ruido de comentarios/notificaciones, timestamp >= not_before_ts
    y exigir identidad: fbid de la foto o el `needle` dentro del texto. Devuelve
    el dict ganador o None; sin senal positiva nunca hay ganador.

    Si el timestamp es desconocido (ts<=0) solo se acepta cuando la linea base
    `before_pids` (pids visibles ANTES de pulsar Publicar) existe y NO contiene el
    pid del candidato: id nuevo. Sin baseline (`before_pids=None`) se rechaza,
    igual que antes (fail-closed)."""
    gid = str(gid or "").strip()
    if not gid:
        return None
    want_fbid = str(fbid or "").strip()
    want_needle = " ".join(cxtlib.norm(needle or "").split())
    before = None
    if before_pids is not None:
        before = {str(p) for p in before_pids}
    nb = None
    if not_before_ts is not None:
        try:
            nb = int(not_before_ts)
        except (TypeError, ValueError):
            nb = None
        if nb is not None and nb >= 1000000000000:  # epoch en ms -> segundos
            nb //= 1000
    best = None
    best_key = None
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        url = str(c.get("url") or "").strip()
        m = re.search(r"/groups/(\d+)/(?:posts|permalink)/(\d+)", url)
        if not m:
            continue
        cgid, pid = m.group(1), m.group(2)
        if cgid != gid or str(c.get("gid") or cgid) != gid:
            continue
        if str(c.get("pid") or "").strip() not in ("", pid):
            continue
        if re.search(r"[?&](?:comment_id|reply_comment_id|notif_id|notif_t)=", url):
            continue
        try:
            ts = int(c.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts >= 1000000000000:  # epoch en ms -> segundos (ts interno)
            ts //= 1000
        if ts > 0:
            if nb is not None and ts < nb:
                continue
        elif before is not None and pid not in before:
            pass  # timestamp desconocido pero id NUEVO: aceptable
        else:
            continue  # fail-closed: sin baseline (o pid ya existente) se rechaza
        ctext = " ".join(cxtlib.norm(c.get("text") or "").split())
        cfbid = str(c.get("fbid") or "").strip()
        fbid_hit = bool(want_fbid and cfbid == want_fbid)
        needle_hit = bool(want_needle and want_needle in ctext)
        if not (fbid_hit or needle_hit):
            continue
        if str(c.get("status") or "").upper() in ("DRAFT", "SCHEDULED", "ERROR", "FAILED"):
            continue
        score = (2 if fbid_hit else 0) + (1 if needle_hit else 0)
        key = (score, ts, int(pid))
        if best_key is None or key > best_key:
            best_key = key
            best = {"url": "https://www.facebook.com/groups/%s/posts/%s/" % (gid, pid),
                    "pid": pid, "gid": gid, "ts": ts, "text": ctext, "fbid": cfbid,
                    "score": score, "fbid_hit": fbid_hit, "needle_hit": needle_hit}
    return best


def capture_page_png(cxt, tab, path, timeout=45):
    """PNG por CDP DIRECTO (Page.captureScreenshot), sin la op `screenshot`.

    La op del puente se atora 25 s por intento; este camino devuelve en una
    sola llamada. Escribe el archivo y devuelve (ok, info) con bytes reales."""
    r = cxtlib.call(cxt, "cdp", {"tabId": tab, "method": "Page.captureScreenshot",
                                 "params": {"format": "png"}}, timeout)
    data = ((r or {}).get("result") or {}).get("data")
    if not data:
        return False, {"error": (r or {}).get("_err") or "sin-data", "path": path}
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError) as e:
        return False, {"error": "base64: %s" % e, "path": path}
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
    except OSError as e:
        return False, {"error": "write: %s" % e, "path": path}
    return True, {"path": path, "bytes": len(raw)}


def feed_photo_count(cxt, tab, gid, fbid="", needle="", rounds=6, detail=None,
                     author="", group="", shot_path=""):
    """Cuenta copias reales en el FEED del grupo. -1 si no se pudo leer.

    Primero ESPERA a que el cuerpo crezca y exista div[role=feed]; el feed
    perezoso no esta listo al primer intento (antes se leia a los 1.0 s y
    salia 0). Despues sondea arriba y va scrolleando; se queda con el maximo.
    `detail` recibe TODAS las senales por separado (no se suman como posts)."""
    if not gid:
        if detail is not None:
            detail.update({"error": "sin-gid", "readable": False})
        return -1
    url = "https://www.facebook.com/groups/%s/?sorting_setting=CHRONOLOGICAL" % gid
    cxtlib.call(cxt, "navigate", {"tabId": tab, "url": url, "timeout": 60000}, 120)
    t0 = time.time()
    state = None
    while time.time() - t0 < 22.0:
        state = cxtlib.cdp(cxt, tab, JS_FEED_STATE)
        st = state if isinstance(state, dict) else {}
        body = int(st.get("body_len") or 0)
        if st.get("feed") and body >= 800:
            if body >= 2500 or time.time() - t0 >= 10.0:
                break
        time.sleep(0.8)
    shot = None
    if shot_path:
        shot_ok, shot_info = capture_page_png(cxt, tab, shot_path)
        shot = {"screenshot": shot_info, "screenshot_ok": bool(shot_ok)}
    best = -1
    best_info = None
    for i in range(max(1, rounds)):
        info = cxtlib.cdp(cxt, tab, js_feed_probe(gid, fbid, needle, author, group))
        if isinstance(info, dict):
            c = int(info.get("count") or 0)
            if best < 0 or c > best:
                best = c
                best_info = info
            elif best_info is None or int(info.get("body_len") or 0) > int(best_info.get("body_len") or 0):
                best_info = info
            if c > 0:
                break
        if i < max(1, rounds) - 1:
            cxtlib.cdp(cxt, tab, "(()=>{window.scrollBy(0,900);return true;})()")
            time.sleep(1.2)
    if detail is not None:
        if best_info is not None:
            detail.update(best_info)
        else:
            detail.update({"readable": False, "error": "feed-sin-lectura"})
        if shot:
            detail.update(shot)
    return best


def verify_feed(cxt, tab, gid, fbid="", before=None, needle="", author="", group="",
                shot_path="", rounds=6):
    """Veredicto de la prueba por feed: ok SOLO si el conteo sube.

    - `feed-ilegible`: no se pudo leer el feed (cuerpo vacio/sin feed).
    - `sin-prueba`: se leyo, pero no hay incremento respecto a la linea base.
    Traza el veredicto con las senales para que la victoria no sea gratis."""
    t0 = now_ms()
    detail = {}
    after = feed_photo_count(cxt, tab, gid, fbid, needle=needle, rounds=rounds,
                             detail=detail, author=author, group=group,
                             shot_path=shot_path)
    readable = bool(detail.get("readable"))
    if after < 0 or not readable:
        verdict = {"ok": False, "reason": "feed-ilegible", "count": after, "signals": detail}
    elif before is not None and before >= 0 and after > before:
        verdict = {"ok": True, "reason": "feed", "count": after, "signals": detail}
    else:
        verdict = {"ok": False, "reason": "sin-prueba", "count": after, "signals": detail}
    trace(group or "", "verify_feed", now_ms() - t0, verdict["ok"], verdict["reason"], None,
          {"count": after, "before": before, "signals": detail})
    return verdict


def confirm_group_post(cxt, tab, post_url, gid, fbid="", author="", group="", shot_path=""):
    """Confirma UNA copia existente por su permalink del grupo (no es el feed).

    El feed de algunos grupos no lista tus propias publicaciones (comprobado:
    la copia existe y el permalink la muestra, mientras el feed devuelve 0).
    Esta sonda es la confirmacion real para ese caso; nunca se usa como prueba
    de un envio NUEVO, solo para verificar la copia que ya existe."""
    if not post_url or not gid:
        return {"ok": False, "reason": "sin-post", "count": 0, "signals": {}}
    t0 = now_ms()
    cxtlib.call(cxt, "navigate", {"tabId": tab, "url": post_url, "timeout": 60000}, 120)
    deadline = time.time() + 22.0
    sig = {}
    while time.time() < deadline:
        info = cxtlib.cdp(cxt, tab, js_feed_probe(gid, fbid, "", author, group))
        if isinstance(info, dict):
            sig = info
            body = int(info.get("body_len") or 0)
            if body >= 800 and (int(info.get("author_hits") or 0) > 0
                                or int(info.get("fbid_links") or 0) > 0):
                break
        time.sleep(1.0)
    ok = (int(sig.get("body_len") or 0) >= 800
          and int(sig.get("group_hits") or 0) > 0
          and (int(sig.get("author_hits") or 0) > 0
               or int(sig.get("fbid_copies") or 0) > 0
               or int(sig.get("fbid_links") or 0) > 0))
    out = {"ok": ok, "count": 1 if ok else 0,
           "reason": "permalink" if ok else "permalink-ilegible", "signals": sig}
    if shot_path:
        shot_ok, sinfo = capture_page_png(cxt, tab, shot_path)
        out["signals"]["screenshot"] = sinfo
        out["signals"]["screenshot_ok"] = bool(shot_ok)
    trace(group or "", "confirm_post", now_ms() - t0, ok, out["reason"], None,
          {"count": out["count"], "url": post_url, "signals": sig})
    return out


def confirm_new_post_permalink(cxt, tab, gid, needle="", fbid="", group="",
                               not_before_ts=None, budget_s=36.0, before_pids=None):
    """Busca el post NUEVO por permalink cuando el feed no lo prueba.

    Rutas probadas que SI listan el post propio: la pagina del miembro dentro
    del grupo y la busqueda del grupo. Presupuesto acotado; sin candidato nuevo
    devuelve ok=False ('sin-prueba') y jamas convierte la duda en exito."""
    t0 = now_ms()
    gid = str(gid or "").strip()
    sig = {"uid": "", "routes": [], "candidates": 0}
    stats = {"tries": 0, "hit": False, "timed_out": False}
    if not gid:
        trace(group or "", "confirm_post", now_ms() - t0, False, "sin-gid", stats, sig)
        return {"ok": False, "count": 0, "reason": "sin-gid", "signals": sig}
    if not_before_ts is None:
        not_before_ts = int(time.time()) - 300
    fast = fastmode.enabled()
    budget_s = fastmode.permalink_budget_s(budget_s)
    piso = 3.0 if fast else 8.0
    deadline = time.time() + max(piso, float(budget_s or (5.0 if fast else 36.0)))
    uid = ""
    for _ in range(3):
        uid = _me_uid(cxt, tab)
        if uid:
            break
        time.sleep(0.4)
    sig["uid"] = uid
    routes = []
    if uid:
        routes.append(("member", "https://www.facebook.com/groups/%s/user/%s/" % (gid, uid), 18.0))
    q = " ".join((needle or "").split())[:160]
    if q:
        routes.append(("search", "https://www.facebook.com/groups/%s/search/?q=%s"
                       % (gid, quote_plus(q)), 12.0))
    if not routes:
        trace(group or "", "confirm_post", now_ms() - t0, False, "sin-uid", stats, sig)
        return {"ok": False, "count": 0, "reason": "sin-uid", "signals": sig}
    seen = []
    for name, url, cap in routes:
        left = deadline - time.time()
        if left < 3.0:
            break
        nav_ms = int(min(30000, max(1000, left * 1000))) if fast else 30000
        nav_wait = min(45.0, max(2.0, left + 0.5)) if fast else 45
        cxtlib.call(cxt, "navigate", {"tabId": tab, "url": url, "timeout": nav_ms}, nav_wait)
        route_end = min(deadline, time.time() + min(cap, left))
        route = {"route": name, "url": url, "candidates": 0, "winner": ""}
        empty = False
        while time.time() < route_end:
            stats["tries"] += 1
            raw = cxtlib.cdp(cxt, tab, js_post_candidates(gid), timeout=10)
            got = raw if isinstance(raw, list) else []
            if got:
                route["candidates"] = len(got)
                seen.extend(got)
                sig["candidates"] = len(seen)
                win = pick_new_post_permalink(seen, gid, needle=needle,
                                              not_before_ts=not_before_ts, fbid=fbid,
                                              before_pids=before_pids)
                if win:
                    route["winner"] = win["pid"]
                    sig["routes"].append(route)
                    stats["hit"] = True
                    el = now_ms() - t0
                    out = {"ok": True, "count": 1, "reason": "permalink-nuevo",
                           "url": win["url"], "pid": win["pid"], "signals": sig,
                           "candidate": win}
                    trace(group or "", "confirm_post", el, True, "permalink-nuevo", stats,
                          {"url": win["url"], "pid": win["pid"], "gid": gid,
                           "signals": sig, "ms": el, "source": name})
                    return out
            elif name == "member":
                # La ruta member devolvio 0 candidatos: esperar el tope es tiempo
                # perdido. Se corta ya y se pasa a la ruta search.
                empty = True
                break
            time.sleep(1.0)
        if empty:
            route["empty"] = True
        else:
            route["timed_out"] = True
        sig["routes"].append(route)
    stats["timed_out"] = True
    el = now_ms() - t0
    trace(group or "", "confirm_post", el, False, "sin-prueba", stats,
          {"gid": gid, "signals": sig, "ms": el})
    return {"ok": False, "count": 0, "reason": "sin-prueba", "signals": sig}


# ------------------------------------------- aprobacion tardia por admin
# Un grupo puede dejar la publicacion "pendiente de aprobacion" y un admin la
# aprueba despues: el envio quedo `sin-prueba` (honesto) pero el post EXISTE.
# El unico ascenso permitido es: aviso de aprobacion + permalink CONFIRMADO con
# la misma identidad del envio (needle). Sin confirmacion, sigue `sin-prueba`.
_APPROVAL_URL_RE = re.compile(r"/groups/(\d+)/(?:posts|permalink)/(\d+)")
_APPROVAL_TIME_RE = re.compile(r"\.\s*\d+\s*(?:mins?|m|s|h|d)\s*$", re.I)
_APPROVAL_TEXT_RE = re.compile(r"aprob(?:ó|o)\s+tu\s+publicaci[oó]n\s+en\s+(.+)", re.I)


def _clean_group_name(value):
    """Nombre de grupo sin el reloj relativo que FB pega al final (puro)."""
    s = " ".join(str(value or "").split())
    s = _APPROVAL_TIME_RE.sub("", s).strip()
    return s.strip(" .").strip()


def parse_approvals(raw):
    """Normaliza /notifications; puro y fail-closed (dict|str|None -> lista).

    Cada item: {url, gid, group, text}. Descarta lo que no sea un permalink de
    grupo con gid+pid numericos, deduplica por url y recorta el texto."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("items")
    if not isinstance(items, list):
        return []
    out = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        m = _APPROVAL_URL_RE.search(url)
        if not m or url in seen:
            continue
        text = " ".join(str(it.get("text") or "").split())[:200]
        group = _clean_group_name(it.get("group"))
        if not group:
            hit = _APPROVAL_TEXT_RE.search(text)
            group = _clean_group_name(hit.group(1)) if hit else ""
        seen.add(url)
        out.append({"url": url, "gid": m.group(1), "group": group, "text": text})
    return out


def match_approval(notifs, group):
    """Primera notificacion del grupo (acentos/mayusculas aparte); puro."""
    want = cxtlib.norm(_clean_group_name(group))
    if not want:
        return None
    for n in notifs or []:
        if not isinstance(n, dict):
            continue
        got = cxtlib.norm(_clean_group_name(n.get("group")))
        if not got:
            continue
        if got == want or want in got or got in want:
            return n
    return None


def read_approvals(cxt, tab):
    """Lee /notifications UNA vez y devuelve las aprobaciones normalizadas."""
    try:
        cxtlib.call(cxt, "navigate",
                    {"tabId": tab, "url": "https://www.facebook.com/notifications",
                     "timeout": 25000})
        time.sleep(5)
        raw = cxtlib.cdp(cxt, tab, JS_APPROVALS, timeout=25)
    except Exception as e:
        log("   no pude leer las notificaciones: %s" % e)
        return []
    return parse_approvals(raw)


def verdict_approval_permalink(raw, needle="", expect_url=""):
    """Veredicto PURO del permalink aprobado; fail-closed ante toda duda.

    ok SOLO si: el href sigue siendo /groups/<gid>/posts/<pid> (y, si se da
    `expect_url`, el MISMO gid+pid del aviso), no hay texto de error, el autor
    aparece y (con `needle` no vacio) el needle aparece. Devuelve
    {ok, reason, signals} con motivos exactos."""
    sig = {"href": "", "gid": "", "pid": "", "has_author": False,
           "needle": False, "needle_hits": 0,
           "needle_required": bool(cxtlib.norm(needle or "")),
           "err": "", "articles": 0}
    if not isinstance(raw, dict):
        return {"ok": False, "reason": "permalink-ilegible", "signals": sig}
    href = str(raw.get("href") or "").strip()
    sig["href"] = href
    m = _APPROVAL_URL_RE.search(href)
    if not m:
        return {"ok": False, "reason": "permalink-ilegible", "signals": sig}
    sig["gid"], sig["pid"] = m.group(1), m.group(2)
    want = _APPROVAL_URL_RE.search(str(expect_url or ""))
    if want and (want.group(1), want.group(2)) != (m.group(1), m.group(2)):
        return {"ok": False, "reason": "permalink-ilegible", "signals": sig}
    sig["err"] = " ".join(str(raw.get("err") or "").split())[:200]
    if sig["err"]:
        return {"ok": False, "reason": "permalink-nodisponible", "signals": sig}
    sig["has_author"] = bool(raw.get("has_author"))
    if not sig["has_author"]:
        return {"ok": False, "reason": "permalink-ajeno", "signals": sig}
    sig["needle"] = bool(raw.get("needle"))
    try:
        sig["needle_hits"] = int(raw.get("needle_hits") or 0)
        sig["articles"] = int(raw.get("articles") or 0)
    except (TypeError, ValueError):
        pass
    if sig["needle_required"] and not sig["needle"]:
        return {"ok": False, "reason": "aprobado-sin-needle", "signals": sig}
    return {"ok": True, "reason": "permalink-aprobado", "signals": sig}


def confirm_approval_permalink(cxt, tab, url, needle=""):
    """Prueba DIRECTA: el permalink del aviso muestra el post aprobado.

    Unica ruta nueva que navega. Cualquier excepcion se registra y se devuelve
    ok=False `permalink-ilegible`: una duda NUNCA se convierte en exito."""
    t0 = now_ms()
    sig = {"url": str(url or ""), "error": ""}
    try:
        cxtlib.call(cxt, "navigate", {"tabId": tab, "url": url, "timeout": 25000})
        time.sleep(6)
        author = _me_name(cxt, tab)
        raw = cxtlib.cdp(cxt, tab, js_approval_post(author, needle), timeout=25)
    except Exception as e:
        log("   no pude leer el permalink aprobado: %s" % e)
        sig["error"] = str(e)
        trace("", "confirm_approval", now_ms() - t0, False, "permalink-ilegible", None, sig)
        return {"ok": False, "reason": "permalink-ilegible", "signals": sig}
    out = verdict_approval_permalink(raw, needle=needle, expect_url=url)
    out["url"] = str(url or "")
    sig.update(out["signals"])
    trace("", "confirm_approval", now_ms() - t0, out["ok"], out["reason"], None, sig)
    return out


def reconcile_approvals(cxt, tab, results, needle=""):
    """Asciende `sin-prueba` a publicado SOLO con aprobacion + permalink.

    Lee /notifications como maximo UNA vez por llamada. Nunca baja un resultado
    ni convierte la duda en exito: el aviso debe coincidir con el grupo y el
    permalink debe confirmarse con la MISMA identidad del envio (`needle`).
    Devuelve cuantos resultados se ascendieron."""
    pending = [r for r in (results or [])
               if isinstance(r, dict) and not r.get("ok") and r.get("reason") == "sin-prueba"]
    if not pending:
        return 0
    notifs = read_approvals(cxt, tab)
    if not notifs:
        return 0
    n = 0
    for r in pending:
        group = r.get("group") or ""
        hit = match_approval(notifs, group)
        if not hit:
            continue
        gid = str(hit.get("gid") or "").strip()
        if not gid:
            continue
        want_gid = _group_id(group)
        if want_gid and want_gid != gid:
            continue
        url = str(hit.get("url") or "").strip()
        # Prueba DIRECTA desde el permalink del propio aviso; si falla, el
        # fallback historico (miembro/busqueda). Solo asciende si una confirma.
        cp = confirm_approval_permalink(cxt, tab, url, needle=needle)
        if not cp.get("ok"):
            cp = confirm_new_post_permalink(cxt, tab, gid, needle=needle, group=group,
                                            not_before_ts=int(time.time()) - 259200)
        if not cp.get("ok"):
            log("   aprobacion sin permalink confirmado: %s (%s)"
                % (group, cp.get("reason")))
            continue
        confirmed = str(cp.get("url") or url)
        r["ok"] = True
        r["published"] = True
        r["reason"] = "permalink-aprobado"
        r["stage"] = "permalink-aprobado"
        r["approval_url"] = url or confirmed
        log("   APROBADA por admin: %s -> %s" % (group, confirmed))
        n += 1
    return n


def _share_one(cxt, tab, group, dry, text, expect, verify=None, post_url=""):
    """Hasta N intentos del flujo completo (FAST: 1); traza cada paso."""
    attempts = fastmode.max_attempts(3)
    for attempt in range(1, attempts + 1):
        log("  intento %d/%d: abrir menu Compartir" % (attempt, attempts))
        t0 = now_ms()
        st1 = {}
        opened = open_share(cxt, tab, expect, stats=st1)
        trace(group, "open_share", now_ms() - t0, opened,
              "" if opened else "menu", st1)
        if not opened:
            log("  no se abrio el menu Compartir; conservo la ventana del post")
            time.sleep(0.35)
            continue
        log("  eligiendo 'Compartir en un grupo'")
        t0 = now_ms()
        st2 = {}
        opted = open_group_option(cxt, tab, stats=st2)
        trace(group, "group_option", now_ms() - t0, opted,
              "" if opted else "picker", st2)
        if not opted:
            log("  no aparecio el selector de grupos; reintento sin cerrar el post")
            time.sleep(0.35)
            continue
        log("  buscando el grupo: %s" % group)
        needle = "" if (not expect or is_boilerplate_expect(expect)) else expect
        t0 = now_ms()
        st3 = {}
        picked = search_and_select(cxt, tab, group, stats=st3, expect=needle,
                                   post_url=post_url, dry=dry)
        trace(group, "search_select", now_ms() - t0, picked,
              "" if picked else "row", st3)
        if not picked:
            log("  no encontre/comparti el grupo; cierro solo el selector")
            close_share_dialog(cxt, tab)
            continue
        if dry:
            log("  simulacro: grupo seleccionado; no se comparte")
            r = {"ok": True, "dry": True, "published": False,
                 "stage": "dry-grupo-seleccionado", "label": group}
            r["tries"] = attempt
            return r
        log("  pulsando Publicar (simulacro=%s)" % dry)
        t0 = now_ms()
        st4 = {}
        r = submit(cxt, tab, dry, text, stats=st4, verify=verify,
                   share_required=True, share_expect=needle, share_post_url=post_url)
        trace(group, "submit", now_ms() - t0, r.get("ok"),
              r.get("stage") or r.get("reason") or "", st4,
               {"click_hit": st4.get("click_hit"), "proof": st4.get("proof", ""),
                "dry": st4.get("dry"),
                "dialogs": st4.get("dialogs"), "dialogs_closed": st4.get("dialogs_closed"),
                "pending": st4.get("pending"), "pending_hint": st4.get("pending_hint"),
                "feed": st4.get("feed")})
        log("  resultado: %s" % json.dumps(r, ensure_ascii=False))
        r["tries"] = attempt
        return r
    log("  AGOTADO: no se pudo completar")
    return {"ok": False, "stage": "retry-exhausted", "tries": attempts,
            "reason": "retry-exhausted"}


def _share_group(cxt, tab, url, group, dry, text, expect):
    """Orquestacion por grupo: limpia la pila, identifica la publicacion y comparte."""
    t0 = now_ms()
    st0 = {}
    clean = close_all_dialogs(cxt, tab, stats=st0)
    trace(group, "cleanup", now_ms() - t0, clean,
          "" if clean else "dialogos-abiertos", st0,
          {"dialogs_before": st0.get("dialogs_before"),
           "dialogs_after": st0.get("dialogs_after")})
    if not clean:
        log("   ABORTO: quedan dialogos abiertos (no publico a ciegas)")
        return {"ok": False, "stage": "cleanup", "reason": "dialogos-abiertos"}
    verify = None
    if not dry and url:
        gid = _group_id(group)
        fbid = _photo_fbid(url)
        if gid and (fbid or text or not is_boilerplate_expect(expect)):
            needle = "" if is_boilerplate_expect(expect) else (expect or "")
            if fastmode.enabled():
                # FAST: sin linea base ni feed; el permalink es la unica prueba.
                author = ""
                before = None
                log("   modo fast: se omite la linea base del feed")
            else:
                tb = now_ms()
                author = _me_name(cxt, tab)
                before = feed_photo_count(cxt, tab, gid, fbid, needle=needle,
                                          author=author, group=group)
                trace(group, "feed_baseline", now_ms() - tb, before >= 0,
                      "" if before >= 0 else "feed", None, {"feed_before": before})
                log("   feed del grupo antes de publicar: %s copia(s)" % before)
            verify = {"gid": gid, "fbid": fbid,
                      "before": before if before is not None and before >= 0 else None,
                      "author": author, "group": group, "needle": needle}
    # Un visor `photo/?fbid=...` comparte la FOTO, no el post del enlace:
    # se resuelve al permalink canonico antes de identificarlo y compartirlo.
    url_share = url
    if url:
        url_share = resolve_photo_viewer_url(cxt, tab, url)
        if not url_share:
            return {"ok": False, "stage": "identity",
                    "reason": "photo-sin-permalink", "tries": 0}
    exp, bad = _identity(cxt, tab, url_share, expect)
    if bad:
        bad["tries"] = 0
        return bad
    r = _share_one(cxt, tab, group, dry, text, exp, verify=verify, post_url=url_share)
    if r.get("ok") and not dry:
        tc = now_ms()
        stc = {}
        closed = close_all_dialogs(cxt, tab, timeout=6.0, stats=stc)
        trace(group, "cleanup_after", now_ms() - tc, closed,
              "" if closed else "dialogos-abiertos", stc,
              {"dialogs_before": stc.get("dialogs_before"),
               "dialogs_after": stc.get("dialogs_after")})
    return r


# Claves historicas de share_post.py: solo estas se exponen al consumidor.
# `pending` y `approval_url` se agregan al final: los consumidores viejos
# siguen funcionando y `approval_url` puede faltar (solo tras ascender).
_PUBLIC_KEYS = ("ok", "published", "dry", "stage", "label", "x", "y", "reason", "pending",
                "approval_url")


def share_target(cxt, tab, post_url, group, dry, text, expect=""):
    """Un grupo. Devuelve EXACTAMENTE las claves historicas de share_post.py."""
    t0 = now_ms()
    r = _share_group(cxt, tab, post_url, group, dry, text, expect)
    trace(group, "group", now_ms() - t0, bool(r.get("ok")), r.get("stage") or r.get("reason") or "")
    if dry or not r.get("ok"):
        # Un simulacro deja el compositor abierto y un fallo puede dejar la pila
        # viva: el siguiente grupo empezaria sobre un dialogo ajeno.
        t1 = now_ms()
        st = {}
        clean = close_all_dialogs(cxt, tab, stats=st)
        trace(group, "cleanup_end", now_ms() - t1, clean,
              "" if clean else "dialogos-abiertos", st,
              {"dialogs_before": st.get("dialogs_before"),
               "dialogs_after": st.get("dialogs_after")})
    return {k: r[k] for k in _PUBLIC_KEYS if k in r}


def share_to_targets(post_url, targets, dry=True, text="", expect="", delay=None,
                     tab=None, cxt=None):
    """Comparte una publicacion existente a N grupos; un dict por grupo."""
    cxt = cxt or cxtlib.resolve_cxt()
    if tab is None:
        tab = cxtlib.resolve_tab(cxt)
    out = []
    n = len(targets)
    for i, g in enumerate(targets):
        t0 = now_ms()
        r = _share_group(cxt, tab, post_url, g, dry, text, expect)
        ms = now_ms() - t0
        step = dict(r)
        step["group"] = g
        step.setdefault("ok", False)
        step.setdefault("published", bool(r.get("published")))
        step.setdefault("stage", "")
        step.setdefault("label", None)
        step.setdefault("tries", 0)
        step.setdefault("reason", "")
        step["ms"] = ms
        out.append(step)
        trace(g, "group", ms, bool(r.get("ok")), r.get("stage") or r.get("reason") or "")
        if delay and i < n - 1:
            time.sleep(delay)          # delay del usuario, intacto
    return out


# ------------------------------------------------ flujo: compositor (grupos)
COMP_JS = (
    r"(()=>{function all(root,out){for(const e of root.querySelectorAll('*')){out.push(e);if(e.shadowRoot)all(e.shadowRoot,out);}}"
    r"const out=[];const needle=" + json.dumps(COMPOSER_TEXT.lower()) + r";all(document,out);let best=null,bd=1e9;"
    r"for(const e of out){const t=((e.innerText||e.textContent||'')+'').toLowerCase();if(t.indexOf(needle)<0||t.length>80)continue;"
    r"const r=e.getBoundingClientRect();if(r.width>=150&&r.height>=18&&r.height<=140){const a=r.width*r.height;if(a<bd){bd=a;best=e;}}}"
    r"if(!best)return 'none';best.scrollIntoView({block:'center'});const r=best.getBoundingClientRect();"
    r"return {x:r.x,y:r.y,w:r.width,h:r.height};})()"
)

SELL_JS = (
    r"(()=>{function all(root,out){for(const e of root.querySelectorAll('*')){out.push(e);if(e.shadowRoot)all(e.shadowRoot,out);}}"
    r"const N=a=>((a||'')+'').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'');"
    r"const out=[];const needles=" + json.dumps(list(SELL_NEEDLES)) + r";all(document,out);let best=null,bd=1e9;"
    r"for(const e of out){const t=N(e.innerText||e.textContent||'');"
    r"if(!needles.some(n=>t.indexOf(n)>=0)||t.length>80)continue;"
    r"const r=e.getBoundingClientRect();if(r.width>=150&&r.height>=18&&r.height<=140){const a=r.width*r.height;if(a<bd){bd=a;best=e;}}}"
    r"if(!best)return 'none';const r=best.getBoundingClientRect();"
    r"return {x:r.x,y:r.y,w:r.width,h:r.height};})()"
)


def _cli_err(d):
    if isinstance(d, dict) and d.get("_err"):
        return str(d["_err"])
    return None


def _eval(cxt, tab, expr):
    r = cxtlib.call(cxt, "cdp", {"method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True},
                                 "tabId": tab}, 180)
    err = _cli_err(r)
    if err:
        raise CliError(err)
    return r.get("result", {}).get("result", {}).get("value")


def composer_rect(cxt, tab, timeout=8.0):
    """Resta del compositor normal; sondea por condicion hasta `timeout`."""
    t0 = time.time()
    while True:
        v = _eval(cxt, tab, COMP_JS)
        if isinstance(v, dict):
            return v
        if time.time() - t0 >= timeout:
            return None
        time.sleep(TICK)


def sell_probe(cxt, tab, timeout=1.0):
    """Resta del compositor de venta si la pagina lo muestra; None si no."""
    t0 = time.time()
    while True:
        v = _eval(cxt, tab, SELL_JS)
        if isinstance(v, dict):
            return v
        if time.time() - t0 >= timeout:
            return None
        time.sleep(TICK)


def _deepfind(cxt, tab, text, limit=800):
    d = cxtlib.call(cxt, "deepfind", {"text": text, "selector": "*", "visibleOnly": True,
                                      "limit": limit, "tabId": tab}, 180)
    err = _cli_err(d)
    if err:
        raise CliError(err)
    return d.get("items") or []


def find_exact(cxt, tab, text, minw=120, lo=20, hi=95):
    its = [i for i in _deepfind(cxt, tab, text, 800)
           if i.get("text", "").strip().lower() == text.lower()
           and i.get("w", 0) >= minw and lo <= i.get("h", 0) <= hi]
    if not its:
        return None
    btns = [i for i in its if (i.get("role") or "") == "button"]
    if btns:
        its = btns
    its.sort(key=lambda i: i["w"] * i["h"])
    return its[0]


def bring_exact(cxt, tab, text, minw, lo, hi, want_y=300, timeout=4.0):
    """Localiza un boton exacto; sondea por condicion y re-centra si hace falta."""
    t0 = time.time()
    it = None
    while True:
        it = find_exact(cxt, tab, text, minw, lo, hi)
        if it:
            break
        if time.time() - t0 >= timeout:
            return None
        time.sleep(DF_TICK)
    dy = it["y"] - want_y
    if abs(dy) > 40:
        cxtlib.call(cxt, "scroll", {"x": 0, "y": dy, "tabId": tab}, 180)
        it2 = find_exact(cxt, tab, text, minw, lo, hi)
        if it2:
            it = it2
    return it


def post_in_group(cxt, tab, g, text, dry=False):
    """Publica `text` en el grupo `g` (nuevo post via el compositor del grupo).

    Trabaja en la pestana propia sin activarla: el documento puede estar oculto
    y aun asi CDP y los clicks JS funcionan. La visibilidad es solo diagnostico."""
    if cxtlib.cdp(cxt, tab, "document.visibilityState") != "visible":
        log("aviso: pestana en segundo plano (documento oculto); sigo igual")
    cxtlib.clear_draft(cxt, tab)
    log("abro el grupo: %s" % g["name"])
    cxtlib.call(cxt, "navigate", {"tabId": tab, "url": g["href"], "timeout": 45000}, 180)
    poll_js(cxt, tab, JS_READY, timeout=8.0, tick=TICK)     # carga de la pagina
    cxtlib.call(cxt, "scroll", {"x": 0, "y": -200000, "tabId": tab}, 180)
    # Cortocircuito temprano: si ya asoma el compositor de venta, no escribimos.
    if sell_probe(cxt, tab, timeout=1.0):
        log("salto grupo de ventas: %s" % g["name"])
        return {"name": g["name"], "ok": False, "skipped": True, "stage": "venta"}
    comp = composer_rect(cxt, tab, timeout=8.0)
    if not comp:
        # Sin compositor normal (o con el de venta): grupo de compraventa que solo
        # ofrece "Vender algo" y no admite un post normal. Se salta.
        if sell_probe(cxt, tab, timeout=1.0):
            log("salto grupo de ventas: %s" % g["name"])
        else:
            log("sin compositor normal, salto el grupo: %s" % g["name"])
        return {"name": g["name"], "ok": False, "skipped": True, "stage": "venta"}
    pub = None
    for _try in range(3):
        cxtlib.call(cxt, "cdpclick", {"x": int(comp["x"] + comp["w"] / 2),
                                      "y": int(comp["y"] + comp["h"] / 2), "tabId": tab}, 180)
        pub = bring_exact(cxt, tab, PUBLISH_TEXT, 200, 30, 95, timeout=3.0)
        if not pub:
            pub = bring_exact(cxt, tab, PUBLISH_TEXT, 120, 25, 120, timeout=2.0)
        if pub:
            break
        comp = composer_rect(cxt, tab, timeout=3.0) or comp
    if not pub:
        log("no aparecio el dialogo de publicacion")
        cxtlib.call(cxt, "cdpkey", {"key": "Escape", "tabId": tab}, 180)
        return {"name": g["name"], "ok": False, "stage": "dialog"}
    if not (cxtlib.call(cxt, "type", {"value": text, "tabId": tab}, 180) or {}).get("typed"):
        cxtlib.call(cxt, "cdptype", {"text": text, "tabId": tab}, 180)
    pub = bring_exact(cxt, tab, PUBLISH_TEXT, 200, 30, 95, timeout=3.0) or pub
    if dry:
        cxtlib.call(cxt, "cdpkey", {"key": "Escape", "tabId": tab}, 180)
        poll_js(cxt, tab, JS_NONE_OPEN, timeout=1.5, tick=DF_TICK)  # cierre por condicion
        log("simulacro OK")
        return {"name": g["name"], "ok": True, "dry": True,
                "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
    for _attempt in range(2):
        pub = bring_exact(cxt, tab, PUBLISH_TEXT, 200, 30, 95, timeout=3.0) or pub
        cxtlib.call(cxt, "cdpclick", {"x": pub["x"] + pub["w"] // 2,
                                      "y": pub["y"] + pub["h"] // 2, "tabId": tab}, 180)
        t0 = time.time()   # exito = el boton Publicar desaparece (dialogo cerrado)
        while time.time() - t0 < 6.0:
            if not find_exact(cxt, tab, PUBLISH_TEXT, 200, 30, 95):
                log("publicado")
                return {"name": g["name"], "ok": True,
                        "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
            time.sleep(DF_TICK)
    log("no pude confirmar la publicacion")
    return {"name": g["name"], "ok": False, "stage": "verify",
            "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
