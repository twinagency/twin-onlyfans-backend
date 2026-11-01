"""Moxy workflow engine: a step-by-step, rule-first chat workflow with swappable creator personas.

The engine decides WHAT happens on each fan event (reply scenario, next scripted offer, escalate, skip) from
explicit rules and per-creator config. Text comes from a playbook of reply templates rendered in the persona's
voice. An AI rephrase hook exists but is off by default: the workflow never depends on a model to choose a step.

Real creator personas and agency playbooks are loaded by PATH and never committed (this repo is public).
"""
from .persona import Persona, load_persona
from .playbook import Playbook, load_playbook
from .state import FanState, JsonStateStore
from .engine import Decision, Engine

__all__ = ["Persona", "load_persona", "Playbook", "load_playbook", "FanState", "JsonStateStore", "Decision", "Engine"]
