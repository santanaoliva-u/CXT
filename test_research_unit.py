from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import research
from editorial_core import Dossier


FIXTURES = Path(__file__).with_name("fixtures") / "rss"
RSS = (FIXTURES / "feed_es.xml").read_text(encoding="utf-8")
ATOM = (FIXTURES / "feed_atom.xml").read_text(encoding="utf-8")
FEED = {"outlet": "Medio de Prueba", "region": "internacional", "language": "es"}


class ResearchUnit(unittest.TestCase):
    def test_parse_rss_extrae_campos(self):
        sources = research.parse_feed(RSS, FEED, "movilidad")
        self.assertEqual(len(sources), 3)
        first = sources[0]
        self.assertEqual(first.title, "Playa del Carmen impulsa un plan de movilidad")
        self.assertEqual(first.url, "https://example.test/playa-movilidad")
        self.assertEqual(first.region, "local")
        self.assertTrue(first.published_at.startswith("2026-10-22"))
        self.assertIn("movilidad", first.excerpt)

    def test_parse_atom_extrae_link_y_fecha(self):
        sources = research.parse_feed(ATOM, FEED, "energía")
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source.url, "https://example.test/ciudades-energia")
        self.assertTrue(source.published_at.startswith("2026-10-22"))
        self.assertIn("ciudades costeras", source.title.lower())

    def test_strip_html(self):
        self.assertEqual(research._plain_html("<p>Hola <b>mundo</b></p>"), "Hola mundo")

    def test_dedupe_por_titulo(self):
        dossier = Dossier(query="movilidad", sources=research.parse_feed(RSS, FEED, "movilidad"))
        dossier.dedupe()
        self.assertEqual(len(dossier.sources), 2)

    def test_rank_prioriza_consulta(self):
        sources = research.parse_feed(RSS, FEED, "movilidad")
        ranked = research._rank(sources, "movilidad en Playa del Carmen", 3)
        self.assertIn("movilidad", ranked[0].title.lower())

    def test_offline_fixture_sin_red(self):
        dossier = research.research("movilidad", offline=True, fixture=FIXTURES, now=1_700_000_000)
        self.assertIsInstance(dossier, Dossier)
        self.assertEqual(dossier.query, "movilidad")
        self.assertEqual(len(dossier.sources), 3)
        self.assertFalse(dossier.cached)
        self.assertTrue(any(s.region == "local" for s in dossier.sources))

    def test_cache_hit_miss_y_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            feed = {"url": "https://feed.test/rss", "outlet": "Feed", "region": "internacional", "language": "es"}
            with mock.patch.object(research, "FEEDS", [feed]), mock.patch.object(
                research, "_fetch_feed", return_value=RSS.encode("utf-8")
            ) as fetch:
                first = research.research("movilidad", now=1000, cache_path=cache)
                self.assertFalse(first.cached)
                self.assertEqual(fetch.call_count, 1)
                second = research.research("movilidad", now=1000 + 899, cache_path=cache)
                self.assertTrue(second.cached)
                self.assertEqual(fetch.call_count, 1)
                third = research.research("movilidad", now=1000 + 901, cache_path=cache)
                self.assertFalse(third.cached)
                self.assertEqual(fetch.call_count, 2)
            self.assertTrue(cache.exists())
            doc = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], research.CACHE_VERSION)

    def test_error_de_feed_no_rompe(self):
        good = {"url": "https://good.test/rss", "outlet": "Bueno", "region": "internacional", "language": "es"}
        bad = {"url": "https://bad.test/rss", "outlet": "Malo", "region": "internacional", "language": "es"}
        with mock.patch.object(research, "FEEDS", [good, bad]), mock.patch.object(
            research, "_fetch_feed", side_effect=[RSS.encode("utf-8"), OSError("caído")]
        ):
            dossier = research.research("movilidad", now=1000, cache_path=None)
        self.assertGreaterEqual(len(dossier.sources), 1)
        self.assertTrue(any("Malo" in error for error in dossier.errors))

    def test_sin_fuentes_no_cachea(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            feed = {"url": "https://empty.test/rss", "outlet": "Vacío", "region": "internacional", "language": "es"}
            with mock.patch.object(research, "FEEDS", [feed]), mock.patch.object(
                research, "_fetch_feed", side_effect=OSError("sin red")
            ):
                dossier = research.research("nada", now=1000, cache_path=cache)
            self.assertEqual(dossier.sources, [])
            self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
