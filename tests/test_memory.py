"""Tests for the typed memory layer — pure SQL round-trips over the eight tables.
Uses the real tmp-db fixture (conftest.py); no network, no LLM.
"""
from praxis import memory
from praxis.models import InverseOp, Plan, SkillContract, Step


def test_record_run_and_steps(db):
    rid = memory.start_run(db, "inst", {"verb": "create"})
    memory.record_step(db, rid, seq=1, intent="x", operation="issues.create",
                       kind="api", status="done", latency_ms=10)
    memory.finish_run(db, rid, status="ok", api_calls=1, llm_calls=1, wall_ms=20, failure_count=0)
    row = db.execute("SELECT status, api_calls FROM runs WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ok" and row["api_calls"] == 1
    step = db.execute("SELECT operation, status FROM run_steps WHERE run_id=?", (rid,)).fetchone()
    assert step["operation"] == "issues.create" and step["status"] == "done"


def test_ref_cache_resolve_once(db):
    memory.put_ref(db, "label:bug", "label", "LA_123", run_id=1)
    assert memory.get_ref(db, "label:bug") == "LA_123"
    assert memory.get_ref(db, "label:missing") is None


def test_plan_put_and_get_roundtrip(db):
    plan = Plan(signature="sig", steps=[
        Step(seq=1, intent="make", operation="issues.create", kind="api", args={"title": "t"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "bug"}),
    ])
    memory.put_plan(db, "sig", plan, cost_api=2, cost_llm=1, wall_ms=50, run_id=1)
    got = memory.get_plan(db, "sig")
    assert got == plan
    assert memory.get_plan(db, "absent") is None


def test_bump_op_stats_tracks_uses_and_average(db):
    memory.bump_op_stats(db, "issues.create", success=True, latency_ms=100)
    memory.bump_op_stats(db, "issues.create", success=False, latency_ms=300)
    row = db.execute("SELECT uses, successes, failures, avg_latency_ms FROM op_stats WHERE operation=?",
                     ("issues.create",)).fetchone()
    assert row["uses"] == 2 and row["successes"] == 1 and row["failures"] == 1
    assert row["avg_latency_ms"] == 200.0


def test_rules_for_returns_added_rule(db):
    memory.add_rule(db, "issues.add_label", "precondition",
                    {"action": "labels.ensure", "param": "label"}, learned_in_run=1)
    rules = memory.rules_for(db, "issues.add_label")
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "precondition"
    assert rules[0]["detail"] == {"action": "labels.ensure", "param": "label"}
    assert rules[0]["learned_in_run"] == 1
    assert memory.rules_for(db, "issues.create") == []


def test_skill_put_and_get_roundtrip(db):
    contract = SkillContract(name="compute.group_by_label", inputs={"issues": "list"},
                             output="markdown", primitives=["rest_get"], test_args={})
    memory.put_skill(db, "compute.group_by_label", contract, "def skill(client):\n    return 1")
    got = memory.get_skill(db, "compute.group_by_label")
    assert got is not None
    assert got["code"].startswith("def skill")
    assert got["status"] == "active"
    assert got["contract"]["name"] == "compute.group_by_label"
    assert memory.get_skill(db, "missing") is None


def test_journal_append_for_and_mark_applied(db):
    a = InverseOp(method="rest_patch", path="/repos/o/r/issues/1", body={"state": "closed"})
    b = InverseOp(method="rest_delete", path="/repos/o/r/labels/bug")
    memory.journal_append(db, run_id=7, seq=1, inverse=a)
    memory.journal_append(db, run_id=7, seq=2, inverse=b)
    entries = memory.journal_for(db, 7)
    assert [e["seq"] for e in entries] == [1, 2]                 # forward order; caller reverses
    assert entries[0]["inverse"] == a and entries[1]["inverse"] == b
    memory.mark_applied(db, entries[0]["id"])
    applied = db.execute("SELECT applied FROM undo_journal WHERE id=?", (entries[0]["id"],)).fetchone()
    assert applied["applied"] == 1
