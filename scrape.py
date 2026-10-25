"""CLI de scraping y exportacion CXT (stdlib + cxtlib + exporters).

Ejecuta las ops `scrape.*` sobre la pestana indicada (o la unica activa) y
exporta Markdown, TXT, CSV o PDF. Tambien acepta un bundle offline con
`--page-json` para pruebas sin extension.

Codigos de salida: 0 ok, 1 uso/args, 2 datos faltantes (NO_TAB/TAB_AMBIGUA/
MISSING_OP), 3 PDF_UNAVAILABLE, 4 escritura, 5 error de extension.
"""
import argparse
import datetime
import html as htmlmod
import json
import os
import sys

import cxtlib
from exporters import apply_limits, LIMITS, slugify, to_markdown, to_txt, tables_to_csv, to_pdf, write_atomic

OP_NAMES = ("page", "markdown", "tables", "links")
FORMATS = ("md", "txt", "csv", "pdf")

MAX_ARGS = {"page": LIMITS["markdown"], "markdown": LIMITS["markdown"],
            "tables": LIMITS["table_rows"], "links": LIMITS["links"]}


class _Parser(argparse.ArgumentParser):
    """argparse con exit 1 (el plan reserva el 2 para datos faltantes)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        raise SystemExit(1)


def _split(valor):
    return [x.strip() for x in (valor or "").split(",") if x.strip()]


def _parse(argv):
    p = _Parser(prog="scrape.py",
                description="Extrae y exporta paginas via CXT (md, txt, csv, pdf)")
    p.add_argument("--url", default=None,
                   help="navega a esta URL antes de extraer")
    p.add_argument("--tab", type=int, default=None,
                   help="id de pestana (si falta: la unica activa)")
    p.add_argument("--ops", default="page,markdown,tables,links",
                   help="ops a ejecutar: page,markdown,tables,links")
    p.add_argument("--format", default="md",
                   help="formatos de salida: md, txt, csv, pdf (separados por coma)")
    p.add_argument("--out", default=None,
                   help="directorio de salida (defecto: $CXT_DIR/out)")
    p.add_argument("--page-json", default=None,
                   help="bundle offline {page,markdown,tables,links}")
    return p.parse_args(argv)


def _resolve_active(cxt):
    """La unica pestana con active is True; nunca resuelve por URL de Facebook."""
    r = cxtlib.call(cxt, "tabs", {})
    if isinstance(r, list):
        tabs = r
    elif isinstance(r, dict) and isinstance(r.get("tabs"), list):
        tabs = r["tabs"]
    else:
        tabs = []
    activas = [t for t in tabs if isinstance(t, dict) and t.get("active") is True]
    if not activas:
        return None, "NO_TAB"
    if len(activas) > 1:
        ids = ",".join(str(t.get("id", t.get("tabId"))) for t in activas)
        return None, "TAB_AMBIGUA: ids " + ids
    t = activas[0]
    return t.get("id", t.get("tabId")), None


def _error_extension(r):
    return (not isinstance(r, dict)) or bool(r.get("error")) or bool(r.get("_err")) \
        or r.get("ok") is False


def _texto_pdf(datos):
    if "markdown" in datos:
        d, t = apply_limits("markdown", datos["markdown"])
        return d.get("markdown") or "", t
    d, t = apply_limits("page", datos["page"])
    return d.get("text") or "", t


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    ops = list(dict.fromkeys(_split(args.ops)))
    if not ops:
        print("error: --ops vacio", file=sys.stderr)
        return 1
    for op in ops:
        if op not in OP_NAMES:
            print("error: op desconocida: %s" % op, file=sys.stderr)
            return 1

    formatos = list(dict.fromkeys(_split(args.format)))
    if not formatos:
        print("error: --format vacio", file=sys.stderr)
        return 1
    for fmt in formatos:
        if fmt not in FORMATS:
            print("error: formato desconocido: %s" % fmt, file=sys.stderr)
            return 1

    salida = args.out or os.path.join(cxtlib.resolve_dir(), "out")
    datos = {}

    if args.page_json:
        try:
            with open(args.page_json, "r", encoding="utf-8") as f:
                bundle = json.load(f)
        except (OSError, ValueError) as e:
            print("error: no se pudo leer --page-json: %s" % e, file=sys.stderr)
            return 1
        for op in ops:
            if not isinstance(bundle, dict) or not isinstance(bundle.get(op), dict):
                print("MISSING_OP:%s" % op, file=sys.stderr)
                return 2
            datos[op] = bundle[op]
    else:
        cxt = cxtlib.resolve_cxt()
        tab = args.tab
        if tab is None:
            tab, err = _resolve_active(cxt)
            if err:
                print(err, file=sys.stderr)
                return 2
        if args.url:
            nr = cxtlib.call(cxt, "navigate", {"tabId": tab, "url": args.url})
            if _error_extension(nr):
                print(json.dumps(nr, ensure_ascii=False), file=sys.stderr)
                return 5
        for op in ops:
            r = cxtlib.call(cxt, "scrape." + op,
                            {"tabId": tab, "max": MAX_ARGS[op]})
            if _error_extension(r):
                print(json.dumps(r, ensure_ascii=False), file=sys.stderr)
                return 5
            datos[op] = r

    for fmt in formatos:
        if fmt == "md" and "markdown" not in datos:
            print("MISSING_OP:markdown", file=sys.stderr)
            return 2
        if fmt == "txt" and "page" not in datos:
            print("MISSING_OP:page", file=sys.stderr)
            return 2
        if fmt == "csv" and "tables" not in datos:
            print("MISSING_OP:tables", file=sys.stderr)
            return 2
        if fmt == "pdf" and "markdown" not in datos and "page" not in datos:
            print("MISSING_OP:page", file=sys.stderr)
            return 2

    titulo = ""
    for op in ops:
        if datos[op].get("title"):
            titulo = datos[op]["title"]
            break
    if not titulo:
        titulo = "pagina"

    try:
        os.makedirs(salida, exist_ok=True)
    except OSError as e:
        print("escritura: %s" % e, file=sys.stderr)
        return 4

    base = slugify(titulo) + "-" + datetime.datetime.now().strftime("%Y%m%d-%H%M")
    archivos = []
    truncado = False
    for fmt in formatos:
        ruta = os.path.join(salida, base + "." + fmt)
        try:
            if fmt == "md":
                d, t = apply_limits("markdown", datos["markdown"])
                truncado = truncado or t
                write_atomic(ruta, (d.get("markdown") or "").encode("utf-8"))
            elif fmt == "txt":
                d, t = apply_limits("page", datos["page"])
                truncado = truncado or t
                write_atomic(ruta, to_txt(d.get("text") or "").encode("utf-8"))
            elif fmt == "csv":
                d, t = apply_limits("tables", datos["tables"])
                truncado = truncado or t
                write_atomic(ruta, tables_to_csv(d.get("tables") or []).encode("utf-8"))
            else:
                texto, t = _texto_pdf(datos)
                truncado = truncado or t
                doc = ('<!doctype html><meta charset="utf-8"><title>%s</title>'
                       '<pre style="white-space:pre-wrap;font:14px/1.5 sans-serif">%s</pre>'
                       % (htmlmod.escape(titulo), htmlmod.escape(texto)))
                to_pdf(doc, ruta)
        except OSError as e:
            print("escritura: %s" % e, file=sys.stderr)
            return 4
        except RuntimeError as e:
            if "PDF_UNAVAILABLE" in str(e):
                print(str(e), file=sys.stderr)
                return 3
            print("error: %s" % e, file=sys.stderr)
            return 4
        archivos.append({"path": ruta, "bytes": os.path.getsize(ruta), "format": fmt})

    print(json.dumps({"files": archivos, "truncated": truncado}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
