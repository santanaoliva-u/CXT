"""Exportadores de scraping CXT (Markdown, TXT, CSV y PDF) sin dependencias nuevas.

Solo biblioteca estandar. El PDF se genera con un motor local (chromium o
pandoc+tectonic/xelatex) y siempre de forma atomica: se escribe a `.tmp` y se
publica con `os.replace`.
"""
import csv
import os
import re
import shutil
import subprocess
import unicodedata
from html.parser import HTMLParser

LIMITS = {"markdown": 200000, "table_rows": 500, "table_cols": 50, "links": 2000}

PDF_ENGINES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
PDF_FALLBACK = ("tectonic", "xelatex")


def slugify(s):
    """Slug estable para nombres de fichero (misma semantica que la extension)."""
    s = unicodedata.normalize("NFD", s or "")
    s = re.sub(r"[\u0300-\u036f]", "", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = s[:60].strip("-")
    return s or "pagina"


class _MarkdownParser(HTMLParser):
    """Convierte el subconjunto de HTML de T2 a Markdown GFM."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.pre = 0
        self.lists = []
        self.href = None
        self.table = None
        self.row = None
        self.cell = None
        self.cells = None

    # -- utilidades ---------------------------------------------------------
    def _emit(self, s):
        self.parts.append(s)

    def _block(self):
        self._emit("\n\n")

    def _last(self):
        return "".join(self.parts)

    def _finish_table(self):
        if not self.table:
            self.table = None
            self.row = None
            self.cell = None
            return
        rows = []
        for cells, _header in self.table:
            vals = []
            for c in cells:
                v = " ".join((c or "").split()).replace("|", "\\|")
                vals.append(v)
            rows.append(vals)
        if rows:
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            lineas = ["| " + " | ".join(rows[0]) + " |",
                      "| " + " | ".join(["---"] * width) + " |"]
            for r in rows[1:]:
                lineas.append("| " + " | ".join(r) + " |")
            self._block()
            self._emit("\n".join(lineas) + "\n")
        self.table = None
        self.row = None
        self.cell = None

    # -- eventos ------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._block()
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "p":
            self._block()
        elif tag in ("div", "section", "article", "header", "footer", "main"):
            self._block()
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "br":
            self._emit("  \n")
        elif tag == "hr":
            self._block()
            self._emit("---\n")
        elif tag == "code":
            if not self.pre:
                self._emit("`")
        elif tag == "pre":
            self._block()
            self._emit("```\n")
            self.pre += 1
        elif tag == "blockquote":
            self._block()
            self._emit("> ")
        elif tag == "ul":
            self._block()
            self.lists.append(["ul", 0])
        elif tag == "ol":
            self._block()
            self.lists.append(["ol", 0])
        elif tag == "li":
            if self._last() and not self._last().endswith("\n"):
                self._emit("\n")
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self._emit("%d. " % self.lists[-1][1])
            else:
                self._emit("- ")
        elif tag == "a":
            self.href = a.get("href") or ""
            self._emit("[")
        elif tag == "img":
            self._emit("![%s](%s)" % (a.get("alt") or "", a.get("src") or ""))
        elif tag == "table":
            self._block()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            self.cells = self.row

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "template"):
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p"):
            self._block()
        elif tag in ("div", "section", "article", "header", "footer", "main"):
            self._block()
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code":
            if not self.pre:
                self._emit("`")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._emit("\n```\n")
            self._block()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._block()
        elif tag == "li":
            self._emit("\n")
        elif tag == "a":
            if self.href is not None:
                self._emit("](%s)" % self.href)
                self.href = None
        elif tag in ("td", "th"):
            if self.cell is not None and self.cells is not None:
                self.cells.append("".join(self.cell))
                self.cell = None
        elif tag == "tr":
            if self.row is not None and self.table is not None:
                self.table.append((self.row, False))
                self.row = None
        elif tag == "table":
            self._finish_table()

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.pre:
            self._emit(data)
        else:
            self._emit(re.sub(r"\s+", " ", data))

    def result(self):
        s = "".join(self.parts)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = s.strip()
        return s + "\n" if s else ""


def to_markdown(html_or_text):
    """HTML -> Markdown; el texto plano (sin '<') pasa tal cual con salto final."""
    s = html_or_text or ""
    if "<" not in s:
        return s if s.endswith("\n") else s + "\n"
    p = _MarkdownParser()
    p.feed(s)
    p.close()
    p._finish_table()
    return p.result()


def to_txt(text):
    """Texto plano normalizado con salto final."""
    return (text or "").strip() + "\n"


class _Buf:
    __slots__ = ("chunks",)

    def __init__(self):
        self.chunks = []

    def write(self, s):
        self.chunks.append(s)


def tables_to_csv(tables):
    """Serializa `[{caption,headers,rows}]` a CSV (CRLF, QUOTE_MINIMAL)."""
    if not tables:
        return ""
    buf = _Buf()
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    for i, t in enumerate(tables):
        if i > 0:
            w.writerow([])
        caption = (t or {}).get("caption") or ""
        if caption:
            w.writerow([caption])
        headers = (t or {}).get("headers") or []
        if headers:
            w.writerow(headers)
        for row in (t or {}).get("rows") or []:
            w.writerow(row)
    return "".join(buf.chunks)


def write_atomic(path, data_bytes):
    """Escribe bytes en `path` via `.tmp` en el mismo directorio + os.replace."""
    if isinstance(data_bytes, str):
        data_bytes = data_bytes.encode("utf-8")
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data_bytes)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _which(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def to_pdf(html, out):
    """HTML -> PDF atomico. RuntimeError PDF_UNAVAILABLE si no hay motor."""
    out = os.path.abspath(out)
    directorio = os.path.dirname(out) or "."
    os.makedirs(directorio, exist_ok=True)
    tmp_html = out + ".tmp.html"
    tmp_pdf = out + ".tmp"
    ok = False
    detalle = ""
    try:
        with open(tmp_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        motor = _which(PDF_ENGINES)
        if motor:
            try:
                r = subprocess.run(
                    [motor, "--headless", "--no-sandbox",
                     "--print-to-pdf=" + tmp_pdf, "file://" + tmp_html],
                    capture_output=True, text=True, timeout=120)
                ok = r.returncode == 0 and os.path.isfile(tmp_pdf) and \
                    os.path.getsize(tmp_pdf) > 0
                if not ok:
                    detalle = (r.stderr or r.stdout or "motor sin PDF").strip()[:300]
            except (OSError, subprocess.SubprocessError) as e:
                detalle = "chromium: %s" % e
        if not ok:
            pandoc = shutil.which("pandoc")
            aux = _which(PDF_FALLBACK)
            if pandoc and aux:
                try:
                    r = subprocess.run(
                        [pandoc, tmp_html, "-o", tmp_pdf,
                         "--pdf-engine=" + aux],
                        capture_output=True, text=True, timeout=120)
                    ok = r.returncode == 0 and os.path.isfile(tmp_pdf) and \
                        os.path.getsize(tmp_pdf) > 0
                    if not ok:
                        detalle = (r.stderr or "pandoc sin PDF").strip()[:300]
                except (OSError, subprocess.SubprocessError) as e:
                    detalle = "pandoc: %s" % e
            elif not motor:
                detalle = "sin motor"
        if not ok:
            raise RuntimeError("PDF_UNAVAILABLE: instala chromium o "
                               "pandoc+tectonic" + ((" (" + detalle + ")") if detalle else ""))
        os.replace(tmp_pdf, out)
    finally:
        for p in (tmp_html, tmp_pdf):
            try:
                os.unlink(p)
            except OSError:
                pass


def apply_limits(op, data):
    """Aplica los topes de T1 a un payload de `scrape.*`.

    Devuelve `(data2, truncated)`. Conserja `chars` original al recortar texto.
    """
    d = dict(data or {})
    truncated = False
    if op == "page":
        texto = d.get("text") or ""
        if len(texto) > LIMITS["markdown"]:
            d["text"] = texto[:LIMITS["markdown"]]
            truncated = True
    elif op == "markdown":
        md = d.get("markdown") or ""
        if len(md) > LIMITS["markdown"]:
            d["markdown"] = md[:LIMITS["markdown"]]
            truncated = True
    elif op == "tables":
        filas_libres = LIMITS["table_rows"]
        cols = LIMITS["table_cols"]
        salida = []
        for t in d.get("tables") or []:
            t = t or {}
            headers = list(t.get("headers") or [])
            if len(headers) > cols:
                headers = headers[:cols]
                truncated = True
            filas = []
            for row in t.get("rows") or []:
                if filas_libres <= 0:
                    truncated = True
                    break
                row = list(row or [])
                if len(row) > cols:
                    row = row[:cols]
                    truncated = True
                filas.append(row)
                filas_libres -= 1
            salida.append({"caption": t.get("caption") or "",
                           "headers": headers, "rows": filas})
        d["tables"] = salida
        if truncated:
            d["truncated"] = True
    elif op == "links":
        links = list(d.get("links") or [])
        if len(links) > LIMITS["links"]:
            d["links"] = links[:LIMITS["links"]]
            truncated = True
            d["truncated"] = True
    return d, truncated
