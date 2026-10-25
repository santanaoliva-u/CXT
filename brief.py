"""Brief verificable de la mesa editorial CXT."""
from __future__ import annotations

from editorial_core import Assignment


def build_brief(assignment: Assignment) -> dict:
    checklist: list[str] = []
    red_lines: list[str] = []

    if assignment.must_include:
        checklist.append("Incluye: " + "; ".join(assignment.must_include))
    if assignment.must_not:
        red_lines.append("No menciones: " + "; ".join(assignment.must_not))
    if assignment.requires_sources:
        checklist.append(
            f"Usa al menos {assignment.min_sources} fuentes independientes y frescas "
            f"(máximo {assignment.freshness_hours} h)."
        )
        checklist.append("Cada afirmación de actualidad debe poder rastrearse a una fuente del dossier.")
        red_lines.append("No inventes hechos, cifras, fechas, nombres ni citas.")
    else:
        checklist.append("Usa solo los datos que aparecen en la instrucción y el brief.")
        red_lines.append("No inventes precios, horarios, testimonios ni datos del negocio.")
    checklist.append(f"Extensión máxima: {assignment.max_length} caracteres.")
    if assignment.cta:
        checklist.append(f"Incluye llamada a la acción: {assignment.cta}")
    if assignment.hashtags:
        checklist.append("Hashtags: " + " ".join(assignment.hashtags))

    return {
        "instruction": assignment.instruction,
        "intent": assignment.intent,
        "source_policy": assignment.source_policy,
        "audience": assignment.audience,
        "locale": assignment.locale,
        "tone": assignment.tone,
        "must_include": list(assignment.must_include),
        "must_not": list(assignment.must_not),
        "freshness_hours": assignment.freshness_hours,
        "min_sources": assignment.min_sources,
        "source_mix": list(assignment.source_mix),
        "max_length": assignment.max_length,
        "cta": assignment.cta,
        "hashtags": list(assignment.hashtags),
        "local": assignment.local,
        "focus_terms": list(assignment.focus_terms),
        "checklist": checklist,
        "red_lines": red_lines,
        "requires_sources": assignment.requires_sources,
    }


def render_brief(brief: dict) -> str:
    lines = [
        "BRIEF EDITORIAL",
        f"Objetivo: {brief.get('instruction', '')}",
        f"Intención: {brief.get('intent', '')} | Política de fuentes: {brief.get('source_policy', '')}",
        f"Audiencia: {brief.get('audience', '')} | Tono: {brief.get('tone', '')} | Locale: {brief.get('locale', '')}",
        "",
        "DEBE APARECER:",
    ]
    lines.extend(f"- {item}" for item in brief.get("checklist") or [])
    lines.append("")
    lines.append("LÍNEAS ROJAS:")
    lines.extend(f"- {item}" for item in brief.get("red_lines") or [])
    lines.append("")
    lines.append("REGLA DE ORO: no inventes hechos. Si el dossier no lo sostiene, no lo escribas.")
    return "\n".join(lines)
