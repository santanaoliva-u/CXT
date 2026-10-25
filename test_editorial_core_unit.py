#!/usr/bin/env python3
"""Pruebas offline de los contratos y el sello editorial."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from editorial_core import (
    Dossier,
    Draft,
    Source,
    build_assignment,
    draft_from_model,
    evaluate_stamp,
)


NOW = 1_800_000_000.0


def source(outlet: str, title: str, excerpt: str, *, region: str = "nacional") -> Source:
    published = datetime.fromtimestamp(NOW - 3600, tz=timezone.utc).isoformat()
    return Source(
        title=title,
        outlet=outlet,
        url="https://example.com/" + title.lower().replace(" ", "-"),
        published_at=published,
        excerpt=excerpt,
        region=region,
        confidence="alta",
    )


def news_dossier() -> Dossier:
    return Dossier(
        query="qué está pasando en el mundo",
        sources=[
            source("BBC Mundo", "Gobierno anuncia acuerdo comercial", "El gobierno anunció un acuerdo comercial."),
            source("El País", "Mercados reaccionan al acuerdo", "Los mercados reaccionaron con cautela."),
            source("Reuters", "Acuerdo comercial entra en vigor", "El acuerdo comercial entra en vigor esta semana.", region="internacional"),
        ],
    )


def green_draft() -> Draft:
    return Draft(
        text="El gobierno anunció un acuerdo comercial. La medida busca reducir aranceles. Los mercados reaccionaron con cautela.",
        claims=[
            {"text": "El gobierno anunció un acuerdo comercial.", "sources": ["S1"]},
            {"text": "Los mercados reaccionaron con cautela.", "sources": ["S2"]},
        ],
        angle_id="global",
    )


class EditorialCoreTest(unittest.TestCase):
    def stamp(self, assignment, dossier, draft, previous_posts=None):
        return evaluate_stamp(assignment, dossier, draft, now=NOW, previous_posts=previous_posts)

    def test_actualidad_con_fuentes_frescas_y_claims_es_verde(self):
        assignment = build_assignment("qué está pasando en el mundo")
        stamp = self.stamp(assignment, news_dossier(), green_draft())
        self.assertEqual(stamp.color, "green", stamp.reasons)
        self.assertTrue(stamp.publishable)
        self.assertFalse(stamp.blocked)

    def test_sin_fuentes_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo")
        stamp = self.stamp(assignment, Dossier(query="vacío"), green_draft())
        self.assertEqual(stamp.color, "red")
        self.assertFalse(stamp.publishable)
        self.assertIn("No hay fuentes", " ".join(stamp.reasons))

    def test_sin_borrador_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo")
        stamp = self.stamp(assignment, news_dossier(), None)
        self.assertEqual(stamp.color, "red")
        self.assertIn("Sin borrador", " ".join(stamp.reasons))

    def test_relleno_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo")
        draft = green_draft()
        draft.text = "Más que una pregunta, el acuerdo comercial avanza."
        stamp = self.stamp(assignment, news_dossier(), draft)
        self.assertEqual(stamp.color, "red")
        self.assertIn("relleno", " ".join(stamp.reasons).lower())

    def test_must_include_incumplido_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo", must_include=["aranceles"])
        draft = green_draft()
        draft.text = "El gobierno anunció un acuerdo comercial."
        stamp = self.stamp(assignment, news_dossier(), draft)
        self.assertEqual(stamp.color, "red")
        self.assertIn("Falta cumplir", " ".join(stamp.reasons))

    def test_must_not_presente_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo", must_not=["guerra"])
        draft = green_draft()
        draft.text = "La guerra comercial sigue. El gobierno anunció un acuerdo."
        stamp = self.stamp(assignment, news_dossier(), draft)
        self.assertEqual(stamp.color, "red")
        self.assertIn("prohibido", " ".join(stamp.reasons))

    def test_cifra_inventada_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo")
        draft = green_draft()
        draft.text = "El acuerdo costará 500 millones. El gobierno lo anunció."
        stamp = self.stamp(assignment, news_dossier(), draft)
        self.assertEqual(stamp.color, "red")
        self.assertIn("Cifras", " ".join(stamp.reasons))

    def test_claim_sin_fuente_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo")
        draft = green_draft()
        draft.claims = [{"text": "El gobierno anunció un acuerdo.", "sources": []}]
        stamp = self.stamp(assignment, news_dossier(), draft)
        self.assertEqual(stamp.color, "red")
        self.assertIn("sin fuente", " ".join(stamp.reasons).lower())

    def test_duplicado_es_rojo(self):
        assignment = build_assignment("qué está pasando en el mundo")
        draft = green_draft()
        stamp = self.stamp(assignment, news_dossier(), draft, previous_posts=[draft.text])
        self.assertEqual(stamp.color, "red")
        self.assertIn("demasiado", " ".join(stamp.reasons))

    def test_instruccion_danina_es_rojo(self):
        assignment = build_assignment("cómo destruir a una persona")
        stamp = self.stamp(assignment, news_dossier(), green_draft())
        self.assertEqual(stamp.color, "red")
        self.assertIn("dañar", " ".join(stamp.reasons))

    def test_brief_sin_fuentes_permite_datos_del_brief(self):
        assignment = build_assignment("Mi barbería ofrece corte clásico por 200 pesos")
        draft = Draft(
            text="Corte clásico por 200 pesos. Agenda tu cita.",
            claims=[],
            angle_id="brief",
        )
        stamp = self.stamp(assignment, Dossier(query="brief"), draft)
        self.assertEqual(stamp.color, "green", stamp.reasons)
        self.assertTrue(stamp.publishable)

    def test_brief_con_cifra_inventada_es_rojo(self):
        assignment = build_assignment("Mi barbería ofrece corte clásico por 200 pesos")
        draft = Draft(
            text="Corte clásico por 200 pesos. Tenemos 8 sucursales.",
            claims=[],
            angle_id="brief",
        )
        stamp = self.stamp(assignment, Dossier(query="brief"), draft)
        self.assertEqual(stamp.color, "red")
        self.assertIn("Cifras", " ".join(stamp.reasons))

    def test_dedupe_por_url_y_titulo(self):
        dossier = news_dossier()
        dossier.sources.append(source("BBC Mundo", "Gobierno anuncia acuerdo comercial", "Duplicado"))
        dossier.dedupe()
        self.assertEqual(len(dossier.sources), 3)

    def test_draft_from_model_limpia_claims(self):
        draft = draft_from_model(
            {
                "text": "  Texto   con espacios  ",
                "claims": [
                    {"text": " Claim uno ", "sources": "S1"},
                    {"text": "", "sources": ["S2"]},
                ],
            },
            angle_id="global",
        )
        self.assertEqual(draft.text, "Texto con espacios")
        self.assertEqual(draft.claims, [{"text": "Claim uno", "sources": ["S1"]}])


if __name__ == "__main__":
    unittest.main()
