"""Ángulos editoriales derivados de la instrucción y el dossier."""
from __future__ import annotations

from editorial_core import Angle, Assignment, Dossier, Source

REGIONS = (
    ("global", "Global", "internacional"),
    ("mexico", "México", "mexico"),
    ("local", "Local", "local"),
)


def _freshness_key(source: Source) -> tuple:
    return (-(source.timestamp() or 0), source.url)


def _pick(sources: list[Source], used: set[str], limit: int = 2) -> list[Source]:
    picked: list[Source] = []
    for source in sorted(sources, key=_freshness_key):
        if source.url and source.url not in used:
            picked.append(source)
            if len(picked) >= limit:
                break
    if not picked:
        for source in sorted(sources, key=_freshness_key):
            picked.append(source)
            if len(picked) >= limit:
                break
    for source in picked:
        if source.url:
            used.add(source.url)
    return picked


def build_angles(assignment: Assignment, dossier: Dossier) -> list[Angle]:
    used: set[str] = set()
    angles: list[Angle] = []
    focus = assignment.instruction.strip() or "el tema"

    for angle_id, label, region in REGIONS:
        exact = [s for s in dossier.sources if s.region == region]
        warning = ""
        if not exact:
            warning = f"Sin fuente {label.lower()} en el dossier; el ángulo usa la mejor evidencia disponible."
        picked = _pick(exact or dossier.sources, used)
        source_ids = [s.url for s in picked if s.url]

        if not dossier.sources:
            thesis = f"¿Qué se puede verificar sobre {focus}? Todavía no hay fuentes para sostener una tesis."
            warning = "Sin fuentes: no se puede afirmar nada; solo formular preguntas."
            why_now = "Sin fecha verificable."
        elif not picked:
            thesis = f"¿Qué cambia en {focus} y quién puede confirmarlo?"
            warning = "Sin fuente asignada; el ángulo queda como hipótesis."
            why_now = "Sin fecha verificable."
        else:
            lead = picked[0]
            if angle_id == "local":
                thesis = f"La conexión local: {lead.title} y lo que implica para {focus}."
            elif angle_id == "mexico":
                thesis = f"La arista mexicana: {lead.title} y su peso en {focus}."
            else:
                thesis = f"La lectura global: {lead.title} y su lugar en {focus}."
            why_now = lead.published_at or "Sin fecha verificable."

        angles.append(
            Angle(
                id=angle_id,
                label=label,
                thesis=thesis,
                source_ids=source_ids,
                why_now=why_now,
                warning=warning,
            )
        )
    return angles
