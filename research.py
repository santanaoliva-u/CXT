"""Investigación RSS de la mesa editorial CXT.

Solo biblioteca estándar. No publica, no usa la API de Facebook y no obedece
instrucciones que aparezcan dentro de las noticias.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from editorial_core import Dossier, Source, iso_now, keywords, normalize_key, parse_time

CACHE_TTL = 15 * 60
CACHE_VERSION = 1
FIXTURES = Path(__file__).with_name("fixtures") / "rss"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CXT-Editorial/1.0"
)

FEEDS = [
    {
        "url": "https://news.google.com/rss?hl=es-419&gl=MX&ceid=MX:es-419",
        "outlet": "Google News",
        "region": "mexico",
        "language": "es",
    },
    {
        "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "outlet": "Google News",
        "region": "internacional",
        "language": "en",
    },
    {
        "url": "https://feeds.bbci.co.uk/mundo/rss.xml",
        "outlet": "BBC Mundo",
        "region": "internacional",
        "language": "es",
    },
    {
        "url": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada",
        "outlet": "El País",
        "region": "internacional",
        "language": "es",
    },
]

LOCAL_FEEDS = [
    {
        "url": (
            "https://news.google.com/rss/search?"
            "q=Playa+del+Carmen+OR+Quintana+Roo+OR+Solidaridad&hl=es-419&gl=MX&ceid=MX:es-419"
        ),
        "outlet": "Google News Local",
        "region": "local",
        "language": "es",
    },
]

LOCAL_RE = re.compile(
    r"playa del carmen|quintana roo|solidaridad|riviera maya|canc[uú]n|tulum|cozumel",
    re.IGNORECASE,
)
MEXICO_RE = re.compile(
    r"m[eé]xico|mexican|cdmx|ciudad de m[eé]xico|amlo|sheinbaum|morena|guadalajara|monterrey",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _plain_html(value: str) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:
        return " ".join(re.sub(r"<[^>]+>", " ", value).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(item: ET.Element, names: Iterable[str]) -> str:
    wanted = {n.lower() for n in names}
    for child in list(item):
        if _local(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _item_link(item: ET.Element) -> str:
    for child in list(item):
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "").strip().lower()
        if href and rel in ("", "alternate"):
            return href
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def _parse_date(value: str) -> str:
    ts = parse_time(value)
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _region_for(text: str, feed_region: str, query: str) -> str:
    haystack = f"{query} {text}"
    if LOCAL_RE.search(haystack) or feed_region == "local":
        return "local"
    if MEXICO_RE.search(haystack) or feed_region == "mexico":
        return "mexico"
    return "internacional"


def parse_feed(data: bytes | str, feed: dict, query: str = "") -> list[Source]:
    """Convierte RSS 2.0, RSS 1.0 o Atom en fuentes normalizadas."""
    if isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    else:
        raw = data
    root = ET.fromstring(raw)
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    sources: list[Source] = []
    for item in entries:
        title = _plain_html(_child_text(item, ("title",)))
        url = _item_link(item)
        if not title or not url:
            continue
        summary = _child_text(item, ("description", "summary", "content", "encoded"))
        excerpt = _plain_html(summary)[:500]
        published = _parse_date(
            _child_text(item, ("pubdate", "published", "updated", "date", "created"))
        )
        outlet = _plain_html(_child_text(item, ("source", "author", "creator"))) or feed["outlet"]
        region = _region_for(f"{title} {excerpt} {outlet}", feed.get("region", "internacional"), query)
        confidence = "alta" if len(excerpt) >= 220 else "media"
        warnings: list[str] = []
        if not published:
            warnings.append("sin fecha legible")
        sources.append(
            Source(
                title=title,
                outlet=outlet,
                url=url,
                published_at=published,
                excerpt=excerpt,
                region=region,
                language=feed.get("language", "es"),
                confidence=confidence,
                claim=title,
                warnings=warnings,
            )
        )
    return sources


def _fetch_feed(url: str, timeout: float = 8.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(2_500_000)
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    return payload


def _cache_key(query: str, limit: int) -> str:
    return f"{normalize_key(query)}|{limit}"


def _load_cache(path: Path | None, key: str, now: float) -> Dossier | None:
    if not path or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entry = (doc.get("entries") or {}).get(key)
        if not entry or now - float(entry.get("ts") or 0) > CACHE_TTL:
            return None
        rows = entry.get("sources") or []
        sources = [Source(**row) for row in rows if isinstance(row, dict)]
        if not sources:
            return None
        return Dossier(
            query=str(entry.get("query") or ""),
            sources=sources,
            errors=list(entry.get("errors") or []),
            cached=True,
            fetched_at=str(entry.get("fetched_at") or iso_now()),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save_cache(path: Path | None, key: str, dossier: Dossier, now: float) -> None:
    if not path or not dossier.sources:
        return
    try:
        doc: dict = {"version": CACHE_VERSION, "entries": {}}
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {"version": CACHE_VERSION, "entries": {}}
        if not isinstance(doc, dict):
            doc = {"version": CACHE_VERSION, "entries": {}}
        entries = doc.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            doc["entries"] = entries
        entries[key] = {
            "ts": now,
            "query": dossier.query,
            "errors": list(dossier.errors),
            "fetched_at": dossier.fetched_at,
            "sources": [asdict(s) for s in dossier.sources],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".research-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    except OSError:
        return


def _rank(sources: list[Source], query: str, limit: int) -> list[Source]:
    terms = {normalize_key(term) for term in keywords(query, limit=12)}
    now = time.time()

    def score(source: Source) -> tuple:
        haystack = normalize_key(f"{source.title} {source.excerpt} {source.outlet}")
        overlap = sum(1 for term in terms if term and term in haystack)
        ts = source.timestamp() or 0
        return (overlap, ts if ts <= now + 300 else 0)

    ranked = sorted(sources, key=score, reverse=True)
    return ranked[:limit]


def research(
    query: str,
    *,
    limit: int = 12,
    offline: bool = False,
    fixture: str | Path | None = None,
    now: float | None = None,
    cache_path: str | Path | None = None,
) -> Dossier:
    """Investiga una consulta y devuelve un Dossier; sin red en modo fixture."""
    current = float(now if now is not None else time.time())
    limit = max(1, min(int(limit or 12), 40))
    use_cache = not offline and fixture is None
    cache = Path(cache_path).expanduser() if cache_path else Path.home() / ".cxt" / "research_cache.json"
    key = _cache_key(query, limit)

    if use_cache:
        cached = _load_cache(cache, key, current)
        if cached is not None:
            cached.query = query
            return cached

    errors: list[str] = []
    collected: list[Source] = []
    feeds = list(FEEDS)
    if LOCAL_RE.search(query):
        feeds = LOCAL_FEEDS + feeds

    if offline or fixture is not None:
        root = Path(fixture).expanduser() if fixture else FIXTURES
        files = sorted(root.glob("*.xml")) if root.is_dir() else [root]
        if not files:
            errors.append(f"sin fixtures en {root}")
        for path in files:
            try:
                collected.extend(parse_feed(path.read_bytes(), {"outlet": path.stem, "region": "internacional", "language": "es"}, query))
            except (OSError, ET.ParseError, ValueError) as exc:
                errors.append(f"{path.name}: {type(exc).__name__}")
    else:
        for feed in feeds:
            try:
                collected.extend(parse_feed(_fetch_feed(feed["url"]), feed, query))
            except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError, ValueError) as exc:
                errors.append(f"{feed['outlet']}: {type(exc).__name__}")

    dossier = Dossier(query=query, sources=collected, errors=errors, cached=False, fetched_at=iso_now())
    dossier.dedupe()
    dossier.sources = _rank(dossier.sources, query, limit)

    if use_cache:
        _save_cache(cache, key, dossier, current)
    return dossier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Investiga fuentes RSS para la mesa editorial CXT.")
    parser.add_argument("query", help="tema o instrucción a investigar")
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--offline", action="store_true", help="no usar red")
    parser.add_argument("--fixture", help="archivo o carpeta de fixtures XML")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--cache", help="ruta alternativa de caché")
    parser.add_argument("--no-cache", action="store_true", help="ignorar la caché")
    args = parser.parse_args(argv)

    dossier = research(
        args.query,
        limit=args.limit,
        offline=args.offline,
        fixture=args.fixture,
        cache_path=None if args.no_cache else args.cache,
    )
    if args.json:
        print(json.dumps(dossier.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Consulta: {dossier.query}")
        print(f"Fuentes: {len(dossier.sources)} | cacheadas: {'sí' if dossier.cached else 'no'}")
        for idx, source in enumerate(dossier.sources, 1):
            print(f"[S{idx}] {source.outlet} — {source.title}")
            print(f"     {source.published_at or 'sin fecha'} | {source.region} | {source.url}")
        if dossier.errors:
            print("Avisos:")
            for error in dossier.errors:
                print(f"  - {error}")
    return 0 if dossier.sources or args.offline else 1


if __name__ == "__main__":
    sys.exit(main())
