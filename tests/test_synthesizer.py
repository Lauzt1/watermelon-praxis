"""Synthesizer tests (offline, stubbed LLM) — the spec §7 five-step loop:
reason a contract -> generate code -> compile in the sandbox -> test (by kind) -> register.

The LLM is stubbed so no network/keys are touched: it replays a queued contract dict then
queued code strings, mirroring the real client's semantics (a schema call returns a
validated model; a json_mode=False call returns the raw string).

Two kinds are covered:
  * pure `compute.*` — tested in-memory against `contract.test_args`, no client calls;
  * effectful (`labels.ensure`) — tested against the (fake) client, its inverse captured
    in the journal and replayed to self-clean.
And the failure path: code that won't compile 3x yields a structured failure, no skill row.
"""
from types import SimpleNamespace

from praxis import memory
from praxis.models import Step
from praxis.synthesizer import synthesize
from tests.test_executor import FakeClient


class StubLLM:
    """Replays queued payloads with the real LLM.complete semantics."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.llm_calls = 0
        self.config = SimpleNamespace(model_workhorse="stub-workhorse", model_planner="stub-planner")

    def complete(self, messages, model=None, schema=None, max_tokens=2000,
                 temperature=0.0, json_mode=True):
        self.llm_calls += 1
        payload = self.payloads.pop(0)
        if not json_mode:
            return payload                       # raw code string
        if schema is not None:
            return schema.model_validate(payload)  # validated SkillContract
        return payload


PURE_CONTRACT = {
    "name": "compute.group_by_label_and_render_table",
    "inputs": {"issues": "list of issue dicts each with title and labels"},
    "output": "a markdown table string",
    "primitives": [],
    "test_args": {"issues": [{"title": "A", "labels": ["bug"]},
                             {"title": "B", "labels": ["bug", "ui"]}]},
}
PURE_CODE = (
    "def skill(client, issues):\n"
    "    groups = {}\n"
    "    for it in issues:\n"
    "        for lab in it.get('labels', []):\n"
    "            groups.setdefault(lab, []).append(it.get('title'))\n"
    "    lines = ['| label | issues |', '| --- | --- |']\n"
    "    for lab in sorted(groups):\n"
    "        lines.append('| ' + lab + ' | ' + ', '.join(groups[lab]) + ' |')\n"
    "    return '\\n'.join(lines)\n"
)

EFFECTFUL_CONTRACT = {
    "name": "labels.ensure",
    "inputs": {"name": "the label name to resolve or create"},
    "output": "the label dict",
    "primitives": ["rest_get", "rest_post"],
    "test_args": {"name": "praxis-synth-test"},
}
EFFECTFUL_CODE = (
    "def skill(client, name):\n"
    "    existing = client.rest_get('/repos/' + client.repo + '/labels')\n"
    "    for lab in existing:\n"
    "        if lab.get('name') == name:\n"
    "            return lab\n"
    "    return client.rest_post('/repos/' + client.repo + '/labels', json={'name': name})\n"
)


def test_pure_synthesis_registers_skill_and_returns_callable(db):
    gap = Step(seq=1, intent="group open issues by label and render a table",
               operation="compute.group_by_label_and_render_table", kind="compute", args={})
    llm = StubLLM([PURE_CONTRACT, PURE_CODE])
    result = synthesize(gap, FakeClient(), db, llm)
    assert result.ok, f"expected success, got attempts={result.attempts}"
    assert callable(result.fn), "success must expose a compiled callable"
    # registered, active, code persisted
    row = memory.get_skill(db, "compute.group_by_label_and_render_table")
    assert row is not None and row["status"] == "active"
    assert "def skill" in row["code"]
    # contract reasoned once + code generated once == 2 llm calls
    assert llm.llm_calls == 2
    # the compiled fn really runs the pure transform in-memory
    table = result.fn(None, issues=PURE_CONTRACT["test_args"]["issues"])
    assert "| label |" in table and "bug" in table


def test_effectful_synthesis_self_cleans_and_registers(db):
    gap = Step(seq=1, intent="resolve or create a label",
               operation="labels.ensure", kind="api", args={})
    client = FakeClient()
    llm = StubLLM([EFFECTFUL_CONTRACT, EFFECTFUL_CODE])
    result = synthesize(gap, client, db, llm)
    assert result.ok, f"expected success, got attempts={result.attempts}"
    assert memory.get_skill(db, "labels.ensure") is not None
    # the test created a label then self-cleaned by replaying its inverse (a delete)
    assert client.undo_applied, "effectful synthesis test must self-clean via the journal"
    # after self-clean the journal must not leak into a caller's run
    assert client.journal is None


def test_three_compile_failures_report_cleanly_without_registering(db):
    gap = Step(seq=1, intent="group issues",
               operation="compute.broken", kind="compute", args={})
    bad = "def skill(client, issues):\n    import os\n    return os\n"  # sandbox rejects import
    llm = StubLLM([PURE_CONTRACT, bad, bad, bad])
    result = synthesize(gap, FakeClient(), db, llm)
    assert not result.ok
    assert len(result.attempts) == 3, "all three attempt errors must be reported"
    assert result.fn is None
    assert memory.get_skill(db, "compute.broken") is None, "no skill registered on failure"
    # 1 contract call + 3 code-gen attempts
    assert llm.llm_calls == 4
