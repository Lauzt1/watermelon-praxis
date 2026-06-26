"""Orchestrator wiring test — recall -> plan -> execute -> report -> write-back,
fully offline (FakeClient + a sequenced stub LLM that answers the recall call then the
plan call). Proves the spine end-to-end before the real-API smoke.
"""
from praxis import memory
from praxis.config import Config
from praxis.orchestrator import Orchestrator
from praxis.recall import signature_key
from tests.test_executor import FakeClient

SIG = {"verb": "create", "entity": "issue", "filters": {}, "artifact": "bug"}
PLAN = {"steps": [{"seq": 1, "intent": "create", "operation": "issues.create",
                   "kind": "api", "args": {"title": "Login times out"}}]}


class SeqLLM:
    """Returns queued payloads in order: first the signature, then the plan."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.llm_calls = 0
        self.config = Config()

    def complete(self, messages, **kwargs):
        self.llm_calls += 1
        return self._payloads.pop(0)


def test_orchestrator_runs_end_to_end(db):
    client = FakeClient()
    llm = SeqLLM([SIG, PLAN])
    report = Orchestrator(db, client, llm).run("create a high priority bug issue")

    assert report.status == "ok"
    assert report.metrics["llm_calls"] == 2          # one recall + one plan call
    assert report.metrics["api_calls"] >= 1

    run = db.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "ok"
    assert memory.get_plan(db, signature_key(SIG)) is not None  # winning plan stored


def test_orchestrator_reuses_cached_plan(db):
    # First run computes + caches both the signature (by exact hash) and the winning plan;
    # an identical second run reuses both deterministically, so it makes ZERO LLM calls.
    # (Task 3.2: the exact-hash signature cache stops phrasing drift from flipping reuse.)
    Orchestrator(db, FakeClient(), SeqLLM([SIG, PLAN])).run("create a high priority bug issue")
    llm2 = SeqLLM([])  # no payloads queued: any recall OR plan LLM call would IndexError
    report = Orchestrator(db, FakeClient(), llm2).run("create a high priority bug issue")
    assert report.status == "ok"
    assert llm2.llm_calls == 0                        # signature from ref_cache + plan from cache
