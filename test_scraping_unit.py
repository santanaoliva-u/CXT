#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon, SIN red) del exportador de scraping.

Cubre la logica pura del contrato:
  - LIMITS exacto
  - slugify: acentos, simbolos, vacio y tope de 60
  - to_markdown: HTML -> Markdown y passthrough de texto plano
  - to_txt: normalizacion + salto final
  - tables_to_csv: cabecera, filas, comillas/comas/saltos y lista vacia
  - apply_limits: page, markdown, tables, links y datos vacios idempotentes
  - write_atomic: bytes exactos y sin `.tmp`
  - to_pdf: PDF_UNAVAILABLE sin motor y limpieza de `.tmp` si el motor falla

Uso: python3 test_scraping_unit.py   (exit 0 = PASS)
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import exporters as E  # noqa: E402
import scrape as S  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name
          + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


def _tmp_left(path):
    """Nombres del directorio que parezcan temporales del exportador."""
    return [n for n in os.listdir(path) if n.endswith(".tmp") or ".tmp." in n]


if __name__ == "__main__":
    total = 0

    def case(name, cond, extra=""):
        global total
        total += 1
        check(name, cond, extra)

    # 0. modulos importables ----------------------------------------------
    case("scrape importable con main()", callable(getattr(S, "main", None)))

    # 1. LIMITS exacto -----------------------------------------------------
    case("LIMITS exacto",
         E.LIMITS == {"markdown": 200000, "table_rows": 500,
                      "table_cols": 50, "links": 2000}, E.LIMITS)

    # 2. slugify -----------------------------------------------------------
    case("slugify quita acentos y normaliza",
         E.slugify("Hola, Mundo! áéíóú ñ") == "hola-mundo-aeiou-n",
         E.slugify("Hola, Mundo! áéíóú ñ"))
    case("slugify solo simbolos -> pagina", E.slugify("!!!###") == "pagina",
         E.slugify("!!!###"))
    case("slugify vacio -> pagina", E.slugify("") == "pagina", E.slugify(""))
    case("slugify tope 60", E.slugify("a" * 80) == "a" * 60
         and len(E.slugify("a" * 80)) == 60, E.slugify("a" * 80))

    # 3. to_markdown -------------------------------------------------------
    md = E.to_markdown("<h1>Hola</h1><p>a <b>b</b></p>")
    case("to_markdown HTML -> # Hola", "# Hola" in md, md)
    case("to_markdown HTML -> **b**", "**b**" in md, md)
    case("to_markdown texto plano", E.to_markdown("texto plano") == "texto plano\n",
         repr(E.to_markdown("texto plano")))
    case("to_markdown passthrough JSON",
         E.to_markdown('{"a": 1}') == '{"a": 1}\n',
         repr(E.to_markdown('{"a": 1}')))

    # 4. to_txt ------------------------------------------------------------
    case("to_txt normaliza y acaba en salto",
         E.to_txt("  hola  ") == "hola\n", repr(E.to_txt("  hola  ")))

    # 5. tables_to_csv -----------------------------------------------------
    csv1 = E.tables_to_csv([{"caption": "", "headers": ["a", "b"],
                             "rows": [[1, "2,3"]]}])
    case("tables_to_csv cabecera", csv1.startswith("a,b\r\n"), repr(csv1))
    case("tables_to_csv filas", "1," in csv1, repr(csv1))
    case("tables_to_csv cita comas", '"2,3"' in csv1, repr(csv1))
    csv2 = E.tables_to_csv([{"headers": ["x"], "rows": [['a"b']]}])
    case("tables_to_csv dobla comillas", '"a""b"' in csv2, repr(csv2))
    csv3 = E.tables_to_csv([{"headers": ["x"], "rows": [["a\nb"]]}])
    case("tables_to_csv cita saltos de linea", '"a\nb"' in csv3, repr(csv3))
    case("tables_to_csv lista vacia -> ''", E.tables_to_csv([]) == "",
         repr(E.tables_to_csv([])))

    # 6. write_atomic ------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        dest = os.path.join(td, "salida.bin")
        E.write_atomic(dest, b"\x00\x01hola")
        with open(dest, "rb") as f:
            datos = f.read()
        case("write_atomic escribe bytes exactos", datos == b"\x00\x01hola",
             repr(datos))
        case("write_atomic no deja .tmp", _tmp_left(td) == [], _tmp_left(td))

    # 7. apply_limits: page (mismo tope que markdown, chars intacto) --------
    r, trunc = E.apply_limits("page", {"text": "x" * 200001, "chars": 200001})
    case("apply_limits page recorta a 200000", len(r["text"]) == 200000,
         len(r["text"]))
    case("apply_limits page conserva chars", r["chars"] == 200001, r.get("chars"))
    case("apply_limits page marca truncated", trunc is True, trunc)

    # 8. apply_limits: markdown -------------------------------------------
    r, trunc = E.apply_limits("markdown", {"markdown": "x" * 200001,
                                           "chars": 200001})
    case("apply_limits markdown recorta a 200000",
         len(r["markdown"]) == 200000, len(r["markdown"]))
    case("apply_limits markdown conserva chars", r["chars"] == 200001,
         r.get("chars"))
    case("apply_limits markdown marca truncated", trunc is True, trunc)

    # 9. apply_limits: tables (500 filas x 50 columnas) -------------------
    filas = [["c%d" % j for j in range(60)] for _ in range(501)]
    r, trunc = E.apply_limits("tables", {"tables": [{
        "caption": "t", "headers": ["h%d" % j for j in range(60)],
        "rows": filas}]})
    t = r["tables"][0]
    case("apply_limits tables deja 500 filas", len(t["rows"]) == 500,
         len(t["rows"]))
    case("apply_limits tables recorta a 50 columnas",
         len(t["headers"]) == 50 and all(len(row) == 50 for row in t["rows"]),
         (len(t["headers"]), sorted({len(row) for row in t["rows"]})))
    case("apply_limits tables marca truncated", trunc is True and r.get("truncated") is True,
         (trunc, r.get("truncated")))

    # 10. apply_limits: links ---------------------------------------------
    r, trunc = E.apply_limits("links", {"links": ["u%d" % i for i in range(2001)]})
    case("apply_limits links deja 2000", len(r["links"]) == 2000,
         len(r["links"]))
    case("apply_limits links marca truncated",
         trunc is True and r.get("truncated") is True,
         (trunc, r.get("truncated")))

    # 11. apply_limits sobre datos vacios es idempotente -------------------
    r, trunc = E.apply_limits("tables", {"tables": [], "truncated": False})
    case("apply_limits tables vacio idempotente",
         r["tables"] == [] and trunc is False and r.get("truncated") is False,
         (r, trunc))

    # 12. to_pdf sin motor -> PDF_UNAVAILABLE ------------------------------
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "doc.pdf")
        orig_which = E.shutil.which
        msg = ""
        ok = False
        try:
            E.shutil.which = lambda _: None
            try:
                E.to_pdf("<h1>x</h1>", out)
            except RuntimeError as e:
                msg = str(e)
                ok = msg.startswith("PDF_UNAVAILABLE")
        finally:
            E.shutil.which = orig_which
        case("to_pdf sin motor -> RuntimeError PDF_UNAVAILABLE", ok, msg)
        case("to_pdf sin motor no deja .tmp", _tmp_left(td) == [], _tmp_left(td))

    # 13. to_pdf con motor que lanza -> limpia .tmp ------------------------
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "doc.pdf")
        orig_which = E.shutil.which
        orig_run = E.subprocess.run

        def _raise(*_a, **_k):
            raise RuntimeError("motor roto")

        lanzo = False
        try:
            E.shutil.which = lambda _: "/bin/true"
            E.subprocess.run = _raise
            try:
                E.to_pdf("<h1>x</h1>", out)
            except RuntimeError:
                lanzo = True
        finally:
            E.shutil.which = orig_which
            E.subprocess.run = orig_run
        case("to_pdf con motor roto propaga RuntimeError", lanzo is True, lanzo)
        case("to_pdf con motor roto no deja .tmp", _tmp_left(td) == [],
             _tmp_left(td))

    ok_n = total - len(fails)
    print(("PASS" if not fails else "FAIL") + " %d/%d" % (ok_n, total))
    sys.exit(1 if fails else 0)
