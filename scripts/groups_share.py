"""groups_share.py - publica un texto en varios grupos de Facebook via CXT.

Reutiliza los helpers de cxtlib.py (call/norm/deep/pick/wait_for/clear_draft/
write_status) en vez de duplicarlos. Contrato externo estable:

  CLI    : --cmd <file> | --list | --filter a,b
  job    : {"ids":[...], "filter":[...], "limit":N, "max":N, "dry":bool,
            "interval_sec":S, "text":"..."}
  estado : share_status.json     pid: share.pid

El texto del compositor y la etiqueta del boton de publicar son configurables
por entorno (CXT_SHARE_COMPOSER_TEXT / CXT_SHARE_PUBLISH_TEXT) para no romperse
con cambios de idioma.

Los grupos de compraventa (que solo ofrecen "Vender algo" y no admiten un post
normal) se detectan y se saltan: por categoria (CXT_SHARE_SKIP_CATEGORIES), por
nombre (heuristica) o por el compositor de venta presente en la pagina. La
etiqueta de venta es configurable (CXT_SHARE_SELL_TEXT).
"""
import json
import os
import subprocess
import sys
import time

import cxtlib

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = cxtlib.resolve_cxt()
TAB = None  # se resuelve de forma perezosa: importar el modulo no toca el CLI
GJ = os.path.join(HERE, "groups.json")
STATUS = os.path.join(HERE, "share_status.json")
PID = os.path.join(HERE, "share.pid")

# Textos de UI (localizacion): antes estaban incrustados en el codigo.
COMPOSER_TEXT = os.environ.get("CXT_SHARE_COMPOSER_TEXT") or "escribe algo"
PUBLISH_TEXT = os.environ.get("CXT_SHARE_PUBLISH_TEXT") or "publicar"
# Etiqueta del compositor de VENTA: los grupos de compraventa solo ofrecen
# "Vender algo" y NO aceptan un post de texto normal -> se ignoran y se saltan.
SELL_TEXT = os.environ.get("CXT_SHARE_SELL_TEXT", "vender algo")

# Tope duro de seguridad: un filtro amplio no puede publicar en cientos de grupos.
MAX_DEFAULT = 50

# `call` ligado al binario y a un timeout de 180 s (nunca lanza por timeout).
call = cxtlib.caller(CXT, 180)
norm = cxtlib.norm

# Needles del compositor de venta (case- y acento-insensible): la etiqueta
# configurable mas las variantes conocidas de Facebook.
SELL_NEEDLES = tuple(dict.fromkeys(
    norm(x) for x in (SELL_TEXT, "vender algo", "vende algo") if norm(x)))

# Palabras que, en el NOMBRE del grupo, delatan compraventa/venta. Heuristica
# deliberadamente conservadora: solo estos substrings (nada de palabras
# genericas como "playa", "noticias" o "comunidad").
SALES_WORDS = ("compra", "venta", "compraventa", "vende", "vendiendo",
               "marketplace", "segunda mano", "remato", "remate", "trueque",
               "intercambio", "solo ventas")

# Categorias de groups.json que delatan compraventa/venta (el escaner las asigna
# por palabras clave). Configurable: CXT_SHARE_SKIP_CATEGORIES="compraventa,renta".
SKIP_CATEGORIES = {cxtlib.norm(x) for x in
                   os.environ.get("CXT_SHARE_SKIP_CATEGORIES", "compraventa").split(",")
                   if x.strip()}


def matches_sell_text(text):
    """True si `text` (visible) contiene la etiqueta del compositor de venta."""
    t = norm(text)
    return any(n in t for n in SELL_NEEDLES)


def is_sales_name(name):
    """Heuristica por NOMBRE: True si parece un grupo de compraventa/venta."""
    n = norm(name)
    return any(w in n for w in SALES_WORDS)


def is_sales_category(cat):
    """Heuristica por CATEGORIA: True si la categoria es de compraventa/venta."""
    c = norm(cat or "")
    return c in SKIP_CATEGORIES or "venta" in c


class CliError(RuntimeError):
    """El CLI cxt fallo (p. ej. extension offline): hay que propagarlo, no vaciar."""


def _clierr(d):
    """Devuelve el mensaje de error si la respuesta del CLI lo trae."""
    if isinstance(d, dict) and d.get("_err"):
        return str(d["_err"])
    return None


def _resolve_tab():
    global TAB
    if TAB is None:
        TAB = cxtlib.resolve_tab(CXT)
    return TAB


def _deepfind(text, limit=800):
    """deepfind que distingue 'sin resultados' de 'el CLI fallo'."""
    d = call("deepfind", {"text": text, "selector": "*", "visibleOnly": True,
                          "limit": limit, "tabId": TAB})
    err = _clierr(d)
    if err:
        raise CliError(err)
    return d.get("items") or []


def ensure_groups():
    if os.path.exists(GJ) and os.path.getsize(GJ) > 2:
        return
    scan = os.path.join(HERE, "groups_scan.py")
    reason = "falta groups_scan.py"
    if os.path.exists(scan):
        try:
            subprocess.run([sys.executable, scan, "--reset"], timeout=900)
            reason = "el escaneo no genero groups.json"
        except subprocess.TimeoutExpired:
            reason = "timeout de 900s al escanear grupos"
        except OSError as e:
            reason = "no pude ejecutar el escaneo: %s" % e
    if not (os.path.exists(GJ) and os.path.getsize(GJ) > 2):
        raise SystemExit("no pude escanear grupos: %s" % reason)


def select_groups(ids, filters):
    ensure_groups()
    data = cxtlib.load_json(GJ)
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise SystemExit("groups.json invalido o corrupto: %s" % GJ)
    groups = data["groups"]
    if isinstance(ids, str):
        ids = [x for x in ids.split(",") if x.strip()]
    if ids:
        idset = set(ids)
        return [g for g in groups if g.get("id") in idset]
    if filters:
        kws = [norm(f) for f in filters]
        kws = [k for k in kws if k]
        if kws:
            return [g for g in groups if any(k in norm(g.get("name", "")) for k in kws)]
    return []


def bring(text, minw, lo, hi, want_y, exact=False, tries=8):
    it = cxtlib.wait_for(CXT, TAB, text, tries=tries, interval=0.4, minw=minw,
                         lo=lo, hi=hi, exact=exact)
    if not it:
        return None
    dy = it["y"] - want_y
    if abs(dy) > 40:
        call("scroll", {"x": 0, "y": dy, "tabId": TAB})
        time.sleep(0.5)
        it2 = cxtlib.wait_for(CXT, TAB, text, tries=tries, interval=0.4, minw=minw,
                              lo=lo, hi=hi, exact=exact)
        if it2:
            it = it2
    return it


def find_exact(text, minw=120, lo=20, hi=95):
    its = [i for i in _deepfind(text, limit=800)
           if i.get("text", "").strip().lower() == text.lower()
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


# Composer del grupo: el placeholder es configurable (CXT_SHARE_COMPOSER_TEXT).
COMP_JS = (
    r"(()=>{function all(root,out){for(const e of root.querySelectorAll('*')){out.push(e);if(e.shadowRoot)all(e.shadowRoot,out);}}"
    r"const out=[];const needle=" + json.dumps(COMPOSER_TEXT.lower()) + r";all(document,out);let best=null,bd=1e9;"
    r"for(const e of out){const t=((e.innerText||e.textContent||'')+'').toLowerCase();if(t.indexOf(needle)<0||t.length>80)continue;"
    r"const r=e.getBoundingClientRect();if(r.width>=150&&r.height>=18&&r.height<=140){const a=r.width*r.height;if(a<bd){bd=a;best=e;}}}"
    r"if(!best)return 'none';best.scrollIntoView({block:'center'});const r=best.getBoundingClientRect();"
    r"return {x:r.x,y:r.y,w:r.width,h:r.height};})()"
)


def composer_rect(tries=20):
    for _ in range(tries):
        r = call("cdp", {"method": "Runtime.evaluate",
                         "params": {"expression": COMP_JS, "returnByValue": True},
                         "tabId": TAB})
        err = _clierr(r)
        if err:
            raise CliError(err)
        v = r.get("result", {}).get("result", {}).get("value")
        if isinstance(v, dict):
            return v
        time.sleep(1)
    return None


# Detector del compositor de VENTA: devuelve la recta del elemento cuyo texto
# visible es la etiqueta de venta ("Vender algo"), o None si no existe.
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


def sell_probe(tries=2):
    """Recta del compositor de venta si la pagina lo muestra; None si no."""
    for _ in range(max(1, tries)):
        r = call("cdp", {"method": "Runtime.evaluate",
                         "params": {"expression": SELL_JS, "returnByValue": True},
                         "tabId": TAB})
        err = _clierr(r)
        if err:
            raise CliError(err)
        v = r.get("result", {}).get("result", {}).get("value")
        if isinstance(v, dict):
            return v
        time.sleep(0.8)
    return None


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def post_to_group(g, text, dry=False):
    call("tab.activate", {"tabId": TAB})
    time.sleep(0.6)
    cxtlib.clear_draft(CXT, TAB)
    log("abro el grupo: %s" % g["name"])
    call("navigate", {"tabId": TAB, "url": g["href"], "timeout": 45000})
    time.sleep(4.0)
    call("scroll", {"x": 0, "y": -200000, "tabId": TAB})
    time.sleep(0.8)
    # Cortocircuito temprano: si ya asoma el compositor de venta, no escribimos.
    if sell_probe(tries=1):
        log("salto grupo de ventas: %s" % g["name"])
        return {"name": g["name"], "ok": False, "skipped": True, "stage": "venta"}
    comp = composer_rect()
    if not comp:
        # Sin compositor normal (o con el de venta): grupo de compraventa que
        # solo ofrece "Vender algo" y no admite un post normal. Se salta.
        if sell_probe(tries=1):
            log("salto grupo de ventas: %s" % g["name"])
        else:
            log("sin compositor normal, salto el grupo: %s" % g["name"])
        return {"name": g["name"], "ok": False, "skipped": True, "stage": "venta"}
    pub = None
    for _try in range(3):
        call("cdpclick", {"x": int(comp["x"] + comp["w"] / 2), "y": int(comp["y"] + comp["h"] / 2), "tabId": TAB})
        pub = bring_exact(PUBLISH_TEXT, 200, 30, 95, tries=8)
        if not pub:
            pub = bring_exact(PUBLISH_TEXT, 120, 25, 120, tries=4)
        if pub:
            break
        comp = composer_rect(tries=6) or comp
    if not pub:
        log("no aparecio el dialogo de publicacion")
        call("cdpkey", {"key": "Escape", "tabId": TAB})
        return {"name": g["name"], "ok": False, "stage": "dialog"}
    if not call("type", {"value": text, "tabId": TAB}).get("typed"):
        call("cdptype", {"text": text, "tabId": TAB})
    time.sleep(0.6)
    pub = bring_exact(PUBLISH_TEXT, 200, 30, 95, tries=8) or pub
    if dry:
        call("cdpkey", {"key": "Escape", "tabId": TAB})
        time.sleep(0.5)
        log("simulacro OK")
        return {"name": g["name"], "ok": True, "dry": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
    for _attempt in range(2):
        pub = bring_exact(PUBLISH_TEXT, 200, 30, 95, tries=8) or pub
        call("cdpclick", {"x": pub["x"] + pub["w"] // 2, "y": pub["y"] + pub["h"] // 2, "tabId": TAB})
        for _ in range(30):
            time.sleep(0.7)
            if not find_exact(PUBLISH_TEXT, 200, 30, 95):
                log("publicado")
                return {"name": g["name"], "ok": True, "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}
    log("no pude confirmar la publicacion")
    return {"name": g["name"], "ok": False, "stage": "verify", "pub": [pub["x"], pub["y"], pub["w"], pub["h"]]}


def _fail(st, err, total=None):
    """Cierra el estado con un error claro (conserva todas las claves)."""
    if total is not None:
        st["total"] = total
    st["running"] = False
    st["current"] = None
    st["error"] = err
    st["finished_at"] = time.time()
    st["finished_ms"] = int(time.time() * 1000)
    cxtlib.write_status(STATUS, st)


def job(cmd):
    filters = cmd.get("filter") or []
    if isinstance(filters, str):
        filters = [x for x in filters.split(",") if x.strip()]
    sel = select_groups(cmd.get("ids") or [], filters)
    limit = int(cmd.get("limit") or 0)
    if limit:
        sel = sel[:limit]
    maxn = int(cmd.get("max") or MAX_DEFAULT)
    capped = False
    if maxn > 0 and len(sel) > maxn:
        sel = sel[:maxn]
        capped = True
    dry = bool(cmd.get("dry"))
    interval = float(cmd.get("interval_sec") or 8)
    text = (cmd.get("text") or "").strip()
    st = {"running": True, "dry": dry, "total": len(sel), "done": 0, "published": 0,
          "skipped": 0, "simulated": 0, "started_at": time.time(), "finished_at": 0, "current": None,
          "results": [], "error": "", "started_ms": int(time.time() * 1000),
          "finished_ms": 0, "capped": capped}
    if not text:
        _fail(st, "texto vacio")
        log("texto vacio: no publico (total=%d)" % len(sel))
        sys.exit(1)
    if not sel:
        _fail(st, "sin grupos (revisa filter/ids)", total=0)
        log("sin grupos (revisa filter/ids)")
        sys.exit(1)
    if _resolve_tab() is None:
        _fail(st, "sin pestana de Facebook")
        sys.exit("no encontre pestana de Facebook (abre Facebook o define CXT_TAB)")
    cxtlib.write_status(STATUS, st)
    log("=== COMPARTIR EN GRUPOS: %d | simulacro=%s | tope=%s%s ===" %
        (len(sel), dry, maxn, " (recortado)" if capped else ""))
    for i, g in enumerate(sel):
        st["current"] = g["name"]
        cxtlib.write_status(STATUS, st)
        log("[%d/%d] %s" % (i + 1, len(sel), g["name"]))
        if is_sales_category(g.get("category")):
            # Grupo de compraventa segun su categoria: no se abre ni se publica.
            log("salto grupo de ventas (categoria): %s" % g["name"])
            r = {"name": g["name"], "ok": False, "skipped": True, "stage": "venta",
                 "reason": "categoria"}
        elif is_sales_name(g["name"]):
            # Grupo de compraventa detectado por nombre: no se abre ni se publica.
            log("salto grupo de ventas: %s" % g["name"])
            r = {"name": g["name"], "ok": False, "skipped": True, "stage": "venta"}
        else:
            try:
                r = post_to_group(g, text, dry=dry)
            except CliError as e:
                r = {"name": g["name"], "ok": False, "stage": "cli", "error": str(e)[:160]}
            except Exception as e:
                r = {"name": g["name"], "ok": False, "stage": "exception", "error": str(e)[:120]}
        st["results"].append(r)
        st["done"] += 1
        if r.get("skipped"):
            st["skipped"] += 1
        elif r.get("ok"):
            if dry:
                st["simulated"] += 1
            else:
                st["published"] += 1
        cxtlib.write_status(STATUS, st)
        if i < len(sel) - 1:  # solo entre grupos, nunca tras el ultimo
            time.sleep(interval)
    st["running"] = False
    st["current"] = None
    st["finished_at"] = time.time()
    st["finished_ms"] = int(time.time() * 1000)
    cxtlib.write_status(STATUS, st)
    if dry:
        log("=== FIN: %d/%d simulados (%d saltados) ===" %
            (st.get("simulated", 0), len(sel), st.get("skipped", 0)))
    else:
        log("=== FIN: %d/%d publicados (%d saltados) ===" %
            (st.get("published", 0), len(sel), st.get("skipped", 0)))
    try:
        os.remove(PID)
    except OSError:
        pass


def main():
    if "--cmd" in sys.argv:
        p = sys.argv.index("--cmd")
        with open(sys.argv[p + 1], encoding="utf-8") as f:
            job(json.load(f))
        return
    if "--list" in sys.argv:
        flt = []
        if "--filter" in sys.argv:
            flt = [x for x in sys.argv[sys.argv.index("--filter") + 1].split(",") if x.strip()]
        sel = select_groups([], flt)
        print(json.dumps({"count": len(sel), "names": [g.get("name") for g in sel]}, ensure_ascii=False))
        return
    data = cxtlib.load_json(GJ)
    print(json.dumps({"total": data.get("count"),
                      "categories": {k: len(v) for k, v in (data.get("categories") or {}).items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
