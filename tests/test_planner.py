"""Tests for the planner: NL -> validated typed Steps, and free reuse of a cached plan.
The planner LLM is stubbed; the plans cache uses the real tmp-db fixture.
"""
import pytest

from praxis import memory, planner
from praxis.models import Plan, Step
from praxis.recall import signature_key

SIG = {"verb": "create", "entity": "issue", "filters": {}, "artifact": "bug"}

TWO_STEP_PAYLOAD = {
    "steps": [
        {"seq": 1, "intent": "create the issue", "operation": "issues.create",
         "kind": "api", "args": {"title": "Login times out"}},
        {"seq": 2, "intent": "add the bug label", "operation": "issues.add_label",
         "kind": "api", "args": {"label": "bug"}},
    ]
}


class _StubLLM:
    def __init__(self, payload):
        self._payload = payload
        self.llm_calls = 0

    def complete(self, messages, **kwargs):
        self.llm_calls += 1
        return self._payload


def test_plan_builds_validated_steps_from_llm(db):
    llm = _StubLLM(TWO_STEP_PAYLOAD)
    p = planner.plan("create a high priority bug issue", SIG, db, llm)
    assert isinstance(p, Plan)
    assert [s.operation for s in p.steps] == ["issues.create", "issues.add_label"]
    assert all(isinstance(s, Step) for s in p.steps)
    assert llm.llm_calls == 1


def test_plan_reuses_cached_plan_without_llm(db):
    cached = Plan(signature=signature_key(SIG), steps=[
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={}),
    ])
    memory.put_plan(db, signature_key(SIG), cached, run_id=1)
    llm = _StubLLM(TWO_STEP_PAYLOAD)
    p = planner.plan("create a high priority bug issue", SIG, db, llm)
    assert p == cached
    assert llm.llm_calls == 0  # served from cache, planner never called the model


def test_plan_rejects_operation_outside_taxonomy(db):
    bad = {"steps": [{"seq": 1, "intent": "x", "operation": "issues.frobnicate",
                      "kind": "api", "args": {}}]}
    llm = _StubLLM(bad)
    with pytest.raises(planner.PlannerError):
        planner.plan("do something weird", SIG, db, llm)


def test_plan_accepts_compute_operation(db):
    payload = {"steps": [{"seq": 1, "intent": "group", "operation": "compute.group_by_label",
                          "kind": "compute", "args": {}}]}
    llm = _StubLLM(payload)
    p = planner.plan("group issues by label", SIG, db, llm)
    assert p.steps[0].operation == "compute.group_by_label"
    assert p.steps[0].kind == "compute"
