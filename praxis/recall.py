"""Recall — turns an instruction into reusable keys.

Two paths, by design (spec §5):
  * `exact_hash` — a cheap, deterministic hash of the normalised instruction. Identical
    re-runs hit this and skip the LLM entirely.
  * `signature` — one planner call that canonicalises the instruction into an
    instruction-independent `{verb, entity, filters, artifact}` shape, so semantically
    equivalent (but differently-worded) instructions collapse to the same key. This dict
    is what gets JSON-serialised as the `plans.signature` / `runs.signature_json` key.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SYSTEM_PROMPT = (
    "You canonicalise a GitHub task instruction into a compact, instruction-independent "
    "signature, so semantically equivalent instructions map to the same key. "
    "Respond ONLY with a JSON object with EXACTLY these keys:\n"
    '  "verb": the primary action as a lowercase canonical verb (e.g. "create", "list", '
    '"label", "triage").\n'
    '  "entity": the GitHub entity acted on (e.g. "issue", "label", "milestone").\n'
    '  "filters": an object of normalised selection criteria '
    '(e.g. {"state": "open", "assignee": "none"}); {} if none.\n'
    '  "artifact": the produced/target descriptor (e.g. "high-priority-bug", '
    '"triage-summary"); "" if none.\n'
    "Lowercase all values. Output no other keys and no prose."
)


def _normalise(instruction: str) -> str:
    return re.sub(r"\s+", " ", instruction.casefold().strip())


def exact_hash(instruction: str) -> str:
    """Stable sha256 of the case/whitespace-normalised instruction — the fast path."""
    return hashlib.sha256(_normalise(instruction).encode("utf-8")).hexdigest()


def signature_key(signature: dict[str, Any]) -> str:
    """Deterministic string key for a signature dict — used for `plans.signature`
    lookup and `runs.signature_json`, so equivalent signatures collide on the same row."""
    return json.dumps(signature, sort_keys=True)


def signature(instruction: str, llm: Any) -> dict[str, Any]:
    """One planner call -> the canonical {verb, entity, filters, artifact} signature.

    Missing keys are backfilled so the shape is always complete and JSON-serialisable.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": instruction},
    ]
    raw = llm.complete(messages) or {}
    return {
        "verb": raw.get("verb", ""),
        "entity": raw.get("entity", ""),
        "filters": raw.get("filters", {}) or {},
        "artifact": raw.get("artifact", ""),
    }
