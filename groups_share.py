"""groups_share.py - publica un texto en varios grupos de Facebook via CXT.

El MOTOR de navegador vive en shared.py (esperas por condicion + traza). Este
archivo es la capa CLI/job/estado con el MISMO contrato externo que antes:

  CLI    : --cmd <file> | --list | --filter a,b
  job    : {"ids":[...], "filter":[...], "limit":N, "max":N, "dry":bool,
            "interval_sec":S, "text":"..."}
  estado : share_status.json     pid: share.pid

Los grupos de compraventa (que solo ofrecen "Vender algo" y no admiten un post
normal) se detectan y se saltan: por categoria (CXT_SHARE_SKIP_CATEGORIES), por
nombre (heuristica) o por el compositor de venta presente en la pagina.
"""
import json
import os
import subprocess
import sys
import time

import cxtlib
import shared

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = cxtlib.resolve_cxt()
TAB = None  # se resuelve de forma perezosa: importar el modulo no toca el CLI
GJ = os.path.join(HERE, "groups.json")
STATUS = os.path.join(HERE, "share_status.json")
PID = os.path.join(HERE, "share.pid")

# Textos de UI (localizacion): viven en el motor compartido (mismos defaults).
COMPOSER_TEXT = shared.COMPOSER_TEXT
PUBLISH_TEXT = shared.PUBLISH_TEXT
SELL_TEXT = shared.SELL_TEXT
SELL_NEEDLES = shared.SELL_NEEDLES

# Tope duro de seguridad: un filtro amplio no puede publicar en cientos de grupos.
MAX_DEFAULT = 50

norm = cxtlib.norm

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

# El motor compartido senala el fallo del CLI con su propia excepcion.
CliError = shared.CliError


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


def _resolve_tab():
    global TAB
    if TAB is None:
        TAB = cxtlib.resolve_tab(CXT)
    return TAB


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


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def post_to_group(g, text, dry=False):
    """Delega TODO el trabajo de navegador al motor compartido (shared.py)."""
    return shared.post_in_group(CXT, _resolve_tab(), g, text, dry)


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
            time.sleep(interval)          # delay del usuario, intacto
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
