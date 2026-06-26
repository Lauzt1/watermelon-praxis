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


# Planner-facing vocabulary. NOTE: the *.ensure ops are deliberately NOT offered to the
# planner. They are executor-internal, injected only via a learned precondition rule
# (Phase 3) or synthesis. Keeping them out of the plan is what preserves the learning
# headline: run 1 must emit the BARE issues.add_label so it hits the 422 and learns the
# rule — if the planner pre-empted that with labels.ensure, there'd be nothing to learn.
VOCABULARY_HELP = (
    "Allowed operations (use ONLY these, nothing else):\n"
    "  issues.create        - create an issue (kind: api)\n"
    "  issues.add_label     - add a label to an issue (kind: api)\n"
    "  issues.set_milestone - set an issue's milestone (kind: api)\n"
    "  issues.list          - list/search issues (kind: api)\n"
    "  compute.<name>       - a synthesised in-process transform, e.g.\n"
    "                         compute.group_by_label_and_render_table (kind: compute)\n"
    "\nDo NOT emit labels.ensure, milestones.ensure, or any operation not listed above. "
    "Add labels and milestones DIRECTLY with issues.add_label / issues.set_milestone; "
    "the agent ensures prerequisites (creating a missing label/milestone) automatically.\n"
    "Label/milestone rules:\n"
    "  - issues.create args carry ONLY title, body, assignees - NEVER a labels or milestone "
    "key. Labels and milestones are added as their own later steps.\n"
    "  - Emit exactly ONE label per issues.add_label step (one label = one step), with the "
    'label name in args as {"label": "<name>"}.\n'
    "  - Use canonical label names: a high-priority issue gets the label `priority:high`; an "
    "urgent one `priority:urgent`; a plain bug gets `bug`."
)
