from __future__ import annotations

import unittest

from angles import build_angles
from brief import build_brief, render_brief
from editorial_core import Assignment, Dossier, Source, build_assignment


def source(title: str, outlet: str, url: str, region: str, when: str) -> Source:
    return Source(title=title, outlet=outlet, url=url, region=region, published_at=when, excerpt=title)


class BriefAnglesUnit(unittest.TestCase):
    def test_brief_noticias_incluye_checklist_y_lineas_rojas(self):
        assignment = build_assignment("qué está pasando en el mundo")
        brief = build_brief(assignment)
        self.assertTrue(brief["requires_sources"])
        self.assertTrue(any("fuentes independientes" in item for item in brief["checklist"]))
        self.assertTrue(any("No inventes" in item for item in brief["red_lines"]))
        self.assertEqual(brief["instruction"], assignment.instruction)
        self.assertIn("BRIEF EDITORIAL", render_brief(brief))
        self.assertIn("REGLA DE ORO", render_brief(brief))

    def test_brief_promo_no_exige_fuentes(self):
        assignment = build_assignment("publica algo sobre mi barbería en Playa del Carmen")
        brief = build_brief(assignment)
        self.assertFalse(brief["requires_sources"])
        self.assertTrue(any("instrucción" in item for item in brief["checklist"]))
        self.assertTrue(any("No inventes precios" in item for item in brief["red_lines"]))

    def test_brief_respeta_must_include_y_must_not(self):
        assignment = build_assignment(
            "explica la situación de la vivienda",
            must_include=["precio promedio"],
            must_not=["partidos políticos"],
        )
        rendered = render_brief(build_brief(assignment))
        self.assertIn("precio promedio", rendered)
        self.assertIn("partidos políticos", rendered)

    def test_tres_angulos_con_fuentes_regionales(self):
        assignment = build_assignment("qué está pasando en el mundo")
        dossier = Dossier(
            query="mundo",
            sources=[
                source("Tema global", "Global News", "https://g.test/1", "internacional", "2026-10-22T10:00:00+00:00"),
                source("Tema México", "México News", "https://m.test/1", "mexico", "2026-10-22T11:00:00+00:00"),
                source("Tema Playa", "Local News", "https://l.test/1", "local", "2026-10-22T12:00:00+00:00"),
            ],
        )
        angles = build_angles(assignment, dossier)
        self.assertEqual([a.id for a in angles], ["global", "mexico", "local"])
        self.assertEqual(angles[0].source_ids, ["https://g.test/1"])
        self.assertEqual(angles[1].source_ids, ["https://m.test/1"])
        self.assertEqual(angles[2].source_ids, ["https://l.test/1"])
        self.assertFalse(any(a.warning for a in angles))

    def test_angulo_sin_region_avisa(self):
        assignment = build_assignment("qué está pasando en el mundo")
        dossier = Dossier(
            query="mundo",
            sources=[source("Solo global", "Global", "https://g.test/2", "internacional", "2026-10-22T10:00:00+00:00")],
        )
        angles = build_angles(assignment, dossier)
        self.assertEqual(len(angles), 3)
        self.assertTrue(any("Sin fuente" in a.warning for a in angles[1:]))
        self.assertTrue(angles[0].source_ids)

    def test_dossier_vacio_devuelve_preguntas_y_avisos(self):
        assignment = build_assignment("qué está pasando en el mundo")
        angles = build_angles(assignment, Dossier(query="mundo"))
        self.assertEqual(len(angles), 3)
        self.assertTrue(all(not a.source_ids for a in angles))
        self.assertTrue(all("Sin fuentes" in a.warning for a in angles))
        self.assertTrue(angles[0].thesis.startswith("¿"))

    def test_determinista(self):
        assignment = build_assignment("qué está pasando en el mundo")
        dossier = Dossier(
            query="mundo",
            sources=[
                source("B", "B", "https://b.test/1", "internacional", "2026-10-21T10:00:00+00:00"),
                source("A", "A", "https://a.test/1", "internacional", "2026-10-22T10:00:00+00:00"),
            ],
        )
        first = [a.to_dict() for a in build_angles(assignment, dossier)]
        second = [a.to_dict() for a in build_angles(assignment, dossier)]
        self.assertEqual(first, second)

    def test_assignment_validate_normaliza(self):
        assignment = Assignment(instruction="  hola  ")
        assignment.validate()
        self.assertEqual(assignment.instruction, "hola")
        self.assertGreaterEqual(assignment.max_length, 180)


if __name__ == "__main__":
    unittest.main()
