"""The one canonical operation taxonomy (plan §"Operation taxonomy", spec §5).

`operation` is the instruction-independent join key for op_stats / learned_rules /
ref_cache reuse, so the strings live in exactly one place. Both the planner (allowed
vocabulary) and the executor (dispatch class + failure policy) import from here.
"""
from __future__ import annotations

# Single-endpoint ops: bound directly to ONE primitive by the executor's declarative
# table — deterministic, never synthesised.
API_SINGLE_ENDPOINT: frozenset[str] = frozenset({
    "issues.create",
    "issues.add_label",
    "issues.set_milestone",
    "issues.list",
})

# Compound ops: resolved through the skills registry and synthesised at runtime (Phase 2).
COMPOUND: frozenset[str] = frozenset({
    "labels.ensure",
    "milestones.ensure",
})

# Enrichment ops augment an already-created artifact; their failure is non-fatal (spec §9).
ENRICHMENT_OPS: frozenset[str] = frozenset({
    "issues.add_label",
    "issues.set_milestone",
})

COMPUTE_PREFIX = "compute."


def is_compute(op: str) -> bool:
    """A synthesised in-process transform, e.g. compute.group_by_label_and_render_table."""
    return op.startswith(COMPUTE_PREFIX)


def is_single_endpoint(op: str) -> bool:
    return op in API_SINGLE_ENDPOINT


def is_compound(op: str) -> bool:
    """Compound or compute — anything resolved via the skills registry/synthesizer."""
    return op in COMPOUND or is_compute(op)


def is_known(op: str) -> bool:
    return op in API_SINGLE_ENDPOINT or op in COMPOUND or is_compute(op)


def is_enrichment(op: str) -> bool:
    return op in ENRICHMENT_OPS


# Allowed-vocabulary string for the planner prompt (compute.* is open-ended).
VOCABULARY_HELP = (
    "Allowed operations:\n"
    "  issues.create       — create an issue (kind: api)\n"
    "  issues.add_label    — add an existing label to an issue (kind: api)\n"
    "  issues.set_milestone— set an issue's milestone (kind: api)\n"
    "  issues.list         — list/search issues (kind: api)\n"
    "  labels.ensure       — resolve/create a label, caching its id (kind: api)\n"
    "  milestones.ensure   — resolve/create a milestone, caching it (kind: api)\n"
    "  compute.<name>      — a synthesised in-process transform (kind: compute)\n"
)
