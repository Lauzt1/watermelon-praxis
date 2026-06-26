"""Planner — natural language -> an ordered list of typed Steps.

Two paths (spec §5):
  * a cached `plans` row for this signature -> deserialize and return for free (no LLM);
  * otherwise one planner-model call constrained to the canonical operation vocabulary,
    whose output is validated into `Step`s before it can be executed.

The LLM prompt *body* is tuned against the live model; the allowed vocabulary and the
Step[] schema it must emit are fixed here.
"""
from __future__ import annotations

from typing import Any

from . import memory, operations
from .models import Plan, Step
from .recall import signature_key

_SYSTEM_PROMPT = (
    "You are the planner for a GitHub automation agent. Decompose the user's instruction "
    "into an ordered list of small, typed steps that achieve it via the GitHub API.\n\n"
    + operations.VOCABULARY_HELP
    + "\nRespond ONLY with a JSON object of the form:\n"
    '  {"steps": [{"seq": 1, "intent": "<short why>", "operation": "<from the list above>", '
    '"kind": "api" | "compute", "args": { ... }}, ...]}\n'
    "Rules: use ONLY operations from the list; number seq from 1; put concrete values "
    "(titles, bodies, label names, filters) in args; emit no prose outside the JSON."
)


class PlannerError(Exception):
    """Raised when the model proposes a step outside the operation taxonomy."""


def plan(instruction: str, signature: dict[str, Any], db, llm) -> Plan:
    key = signature_key(signature)

    cached = memory.get_plan(db, key)
    if cached is not None:
        return cached

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": instruction},
    ]
    raw = llm.complete(messages) or {}
    raw_steps = raw.get("steps", []) if isinstance(raw, dict) else []

    steps: list[Step] = []
    for item in raw_steps:
        step = Step.model_validate(item)
        if not operations.is_known(step.operation):
            raise PlannerError(
                f"planner proposed unknown operation {step.operation!r}; "
                "allowed: single-endpoint, *.ensure, or compute.*"
            )
        steps.append(step)

    return Plan(signature=key, steps=steps)
