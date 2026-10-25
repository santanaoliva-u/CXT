"""Perfil del modo rápido de CXT.

El modo rápido acepta el riesgo de ban: elimina el pacing anti-ban y la
verificación cara, pero NO desactiva el kill-switch, la detección de
checkpoint/halt ni el lock del navegador. Se activa con --fast, con
CXT_FAST=1 o con fast:true por ítem de la cola.
"""

import os

_TRUTHY = {"1", "true", "yes", "si", "sí", "on"}

# Perfil numérico FAST. Los valores son inmutables: knobs() devuelve una copia.
PROFILE = {
    "inter_group_delay_s": 1.5,
    "jitter": 0.0,
    "cooldown_h": 0.0,
    "gap_h": 0.0,
    "verify_feed": False,
    "permalink_budget_s": 5.0,
    "max_attempts": 1,
    "min_interval_min": 1.0,
}


def enabled(env=None, item=None):
    """True si el modo rápido está activo por entorno o por ítem.

    `env` permite inyectar un mapping en tests; `item` permite activarlo
    solo para un ítem concreto de la cola.
    """
    if item and bool(item.get("fast")):
        return True
    if env is None:
        env = os.environ
    return str(env.get("CXT_FAST", "")).strip().lower() in _TRUTHY


def knobs():
    """Copia del perfil FAST (números y flags inmutables)."""
    return dict(PROFILE)


def tag(env=None, item=None):
    """Etiqueta para status/traza: 'fast' o 'normal'."""
    return "fast" if enabled(env=env, item=item) else "normal"


def inter_group_delay_s(default):
    return knobs()["inter_group_delay_s"] if enabled() else default


def permalink_budget_s(default=36.0):
    return knobs()["permalink_budget_s"] if enabled() else default


def verify_feed_enabled():
    """En FAST la verificación de feed se omite."""
    return not enabled() or bool(knobs()["verify_feed"])


def max_attempts(default=3):
    return knobs()["max_attempts"] if enabled() else default


def cooldown_h(default):
    return knobs()["cooldown_h"] if enabled() else default


def gap_h(default):
    return knobs()["gap_h"] if enabled() else default
