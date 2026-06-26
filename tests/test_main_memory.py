"""Tests for the `memory` inspection command (the before/after view the brief requires).

`render_memory` is a pure DB-reader/formatter, so it is fully testable offline — no keys,
no network, no argparse.
"""
from praxis import memory
from praxis.main import render_memory
from praxis.models import SkillContract


def _seed(db):
    memory.add_rule(db, "issues.add_label", "precondition",
                    {"action": "labels.ensure", "param": "label"}, learned_in_run=1)
    memory.put_ref(db, "label:priority:high", "label", "200", run_id=1)
    memory.bump_op_stats(db, "issues.add_label", success=True, latency_ms=10)
    memory.put_skill(db, "labels.ensure",
                     SkillContract(name="labels.ensure", inputs={"label": "name"}, output="dict",
                                   primitives=["rest_get", "rest_post"], test_args={}),
                     "def skill(client, label):\n    return label")


def test_render_memory_shows_every_section(db):
    _seed(db)
    out = render_memory(db)
    assert "learned_rules" in out
    assert "ref_cache" in out
    assert "op_stats" in out
    assert "skills" in out
    # the actual learned state is visible
    assert "issues.add_label" in out
    assert "labels.ensure" in out
    assert "label:priority:high" in out
    assert "run #1" in out                      # the rule was learned in run 1


def test_render_memory_filters_by_operation(db):
    _seed(db)
    memory.add_rule(db, "issues.set_milestone", "precondition",
                    {"action": "milestones.ensure", "param": "milestone"}, learned_in_run=2)
    out = render_memory(db, operation="issues.add_label")
    assert "issues.add_label" in out
    assert "issues.set_milestone" not in out    # the unrelated rule is filtered out
