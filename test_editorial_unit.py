from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import editorial
from editorial_core import Dossier, Source, build_assignment


def _dossier(query: str = "qué está pasando en el mundo", empty: bool = False) -> Dossier:
    if empty:
        return Dossier(query=query)
    now = time.time()
    when = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3600))
    return Dossier(
        query=query,
        sources=[
            Source(title="El mundo cambia", outlet="Global", url="https://g.test/1", region="internacional", published_at=when, excerpt="Cambios globales verificables."),
            Source(title="México responde", outlet="México", url="https://m.test/1", region="mexico", published_at=when, excerpt="Respuesta mexicana verificable."),
            Source(title="Playa del Carmen se adapta", outlet="Local", url="https://l.test/1", region="local", published_at=when, excerpt="Adaptación local verificable."),
        ],
    )


def _model(assignment, dossier, angle, brief):
    return {
        "text": "Esto está pasando en el mundo: hay cambios globales y respuestas locales. La evidencia disponible permite explicar qué cambia y para quién. #Mundo #Actualidad",
        "claims": [
            {"text": "hay cambios globales", "sources": ["https://g.test/1"]},
            {"text": "hay respuestas locales", "sources": ["https://l.test/1"]},
        ],
    }


class EditorialUnit(unittest.TestCase):
    def test_generate_verde_con_fuentes_y_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            result = editorial.generate(
                "qué está pasando en el mundo",
                research_fn=lambda *a, **k: _dossier(),
                model_fn=_model,
                db_path=db,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["stamp"]["color"], "green")
            self.assertTrue(result["stamp"]["publishable"])
            self.assertEqual(len(result["angles"]), 3)
            self.assertEqual(result["draft"]["claims"][0]["sources"], ["https://g.test/1"])
            self.assertEqual(len(editorial.list_runs(db)), 1)

    def test_sin_fuentes_no_llama_al_modelo(self):
        called = {"value": False}

        def model(*args, **kwargs):
            called["value"] = True
            return {}

        result = editorial.generate(
            "qué está pasando en el mundo",
            research_fn=lambda *a, **k: _dossier(empty=True),
            model_fn=model,
            db_path=Path(tempfile.mkdtemp()) / "editorial.db",
        )
        self.assertFalse(result["ok"])
        self.assertFalse(called["value"])
        self.assertEqual(result["stamp"]["color"], "red")
        self.assertIsNone(result["draft"])

    def test_research_only_no_redacta(self):
        result = editorial.generate(
            "qué está pasando en el mundo",
            mode="research",
            research_fn=lambda *a, **k: _dossier(),
            model_fn=lambda *a, **k: self.fail("no debe redactar"),
            db_path=Path(tempfile.mkdtemp()) / "editorial.db",
        )
        self.assertIsNone(result["draft"])
        self.assertIsNone(result["stamp"])
        self.assertTrue(result["ok"])

    def test_verify_editado_no_publicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            result = editorial.generate(
                "qué está pasando en el mundo",
                research_fn=lambda *a, **k: _dossier(),
                model_fn=_model,
                db_path=db,
            )
            stamp = editorial.verify_draft(result["run_id"], result["draft"]["text"] + " Editado.", db_path=db)
            self.assertFalse(stamp["publishable"])
            self.assertIn(stamp["color"], ("amber", "red"))
            self.assertFalse(stamp["checks"]["not_edited"]["ok"])

    def test_list_runs_limita_y_ordena(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            for idx in range(3):
                editorial.generate(
                    "qué está pasando en el mundo",
                    research_fn=lambda *a, **k: _dossier(),
                    model_fn=lambda *a, _idx=idx, **k: _model(*a, **k),
                    db_path=db,
                )
            runs = editorial.list_runs(db, limit=2)
            self.assertEqual(len(runs), 2)
            self.assertIn("id", runs[0])

    def test_get_run_y_draft_for_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "editorial.db"
            first = editorial.generate(
                "qué está pasando en el mundo",
                research_fn=lambda *a, **k: _dossier(),
                model_fn=_model,
                db_path=db,
            )
            run = editorial.get_run(first["run_id"], db)
            self.assertEqual(run["run_id"], first["run_id"])
            self.assertTrue(run["draft"]["text"])
            second = editorial.draft_for_run(
                first["run_id"],
                angle_id="local",
                db_path=db,
                model_fn=_model,
            )
            self.assertTrue(second["ok"])
            self.assertEqual(second["draft"]["angle_id"], "local")
            self.assertEqual(second["stamp"]["color"], "green")

    def test_choose_angle_respeta_pedido(self):
        assignment = build_assignment("qué está pasando en el mundo")
        from angles import build_angles

        angles = build_angles(assignment, _dossier())
        self.assertEqual(editorial.choose_angle(angles, "local").id, "local")
        self.assertEqual(editorial.choose_angle(angles).id, "global")


if __name__ == "__main__":
    unittest.main()
