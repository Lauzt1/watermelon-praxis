"""Offline self-check: no network, no API keys. Exit non-zero on any failure."""
import sys, tempfile
from pathlib import Path
from praxis import memory
from praxis.db import connect
from praxis.models import Step, Plan, Report, StepResult
from praxis.platform.github import inverse_of

CHECKS = []
def check(fn): CHECKS.append(fn); return fn

@check
def schema_creates():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "t.db")
        try:
            n = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()  # release the file handle so Windows can clean up the tempdir
        assert {"runs", "skills", "learned_rules", "ref_cache"} <= n, "missing tables"

@check
def models_validate():
    p = Plan(signature="s", steps=[Step(seq=1, intent="x", operation="issues.create", kind="api", args={})])
    assert Plan.model_validate_json(p.model_dump_json()) == p

@check
def inverse_of_roundtrips_five_shapes():
    # create issue -> close
    inv = inverse_of("rest_post", "/repos/o/r/issues", {}, {"number": 42})
    assert inv.method == "rest_patch" and inv.path == "/repos/o/r/issues/42" and inv.body == {"state": "closed"}
    # add label -> remove label
    inv = inverse_of("rest_post", "/repos/o/r/issues/42/labels", {"labels": ["bug"]}, [{"name": "bug"}])
    assert inv.method == "rest_delete" and inv.path == "/repos/o/r/issues/42/labels/bug"
    # set milestone -> clear milestone
    inv = inverse_of("rest_patch", "/repos/o/r/issues/42", {"milestone": 5}, {"number": 42})
    assert inv.method == "rest_patch" and inv.body == {"milestone": None}
    # create label -> delete label
    inv = inverse_of("rest_post", "/repos/o/r/labels", {"name": "priority:high"}, {"name": "priority:high"})
    assert inv.method == "rest_delete" and inv.path == "/repos/o/r/labels/priority:high"
    # create milestone -> delete milestone
    inv = inverse_of("rest_post", "/repos/o/r/milestones", {"title": "Q3"}, {"number": 7})
    assert inv.method == "rest_delete" and inv.path == "/repos/o/r/milestones/7"
    # reads / unknown mutations -> None
    assert inverse_of("rest_get", "/repos/o/r/issues", None, [{"number": 1}]) is None
    assert inverse_of("rest_patch", "/repos/o/r/issues/42", {"title": "x"}, {"number": 42}) is None

@check
def memory_roundtrips():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "m.db")
        try:
            rid = memory.start_run(conn, "inst", {"verb": "create"})
            memory.record_step(conn, rid, seq=1, intent="x", operation="issues.create",
                               kind="api", status="done", latency_ms=5)
            memory.finish_run(conn, rid, status="ok", api_calls=1, llm_calls=1, wall_ms=9, failure_count=0)
            row = conn.execute("SELECT status, api_calls FROM runs WHERE id=?", (rid,)).fetchone()
            assert row["status"] == "ok" and row["api_calls"] == 1, "run not persisted"
            # ref_cache miss then hit
            assert memory.get_ref(conn, "label:bug") is None, "expected cache miss"
            memory.put_ref(conn, "label:bug", "label", "LA_1", run_id=rid)
            assert memory.get_ref(conn, "label:bug") == "LA_1", "expected cache hit"
        finally:
            conn.close()

@check
def executor_enrichment_keeps_primary():
    from praxis.executor import Executor
    from tests.test_executor import FakeClient
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "e.db")
        try:
            client = FakeClient(fail_on_seq=2)
            steps = [Step(seq=1, intent="c", operation="issues.create", kind="api", args={"title": "A"}),
                     Step(seq=2, intent="l", operation="issues.add_label", kind="api", args={"issue": 1, "label": "bug"})]
            results = Executor(conn, client).run(run_id=1, steps=steps)
            assert results[0].status == "done", "primary should be kept"
            assert results[1].status == "failed", "enrichment failure should be reported"
            assert not client.undo_applied, "no rollback on an enrichment failure"
        finally:
            conn.close()

@check
def executor_fatal_rolls_back():
    from praxis.executor import Executor
    from tests.test_executor import FakeClient
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "f.db")
        try:
            client = FakeClient(fail_on_seq=2)
            steps = [Step(seq=1, intent="a", operation="issues.create", kind="api", args={"title": "A"}),
                     Step(seq=2, intent="b", operation="issues.create", kind="api", args={"title": "B"})]
            results = Executor(conn, client).run(run_id=1, steps=steps)
            assert results[0].status == "rolled_back", "prior mutation should roll back"
            assert results[1].status == "failed"
            assert client.undo_applied, "inverse op should be replayed on a fatal failure"
        finally:
            conn.close()

@check
def sandbox_rejects_forbidden_and_runs_clean():
    from praxis.sandbox import compile_skill, run_skill
    forbidden = [
        "def skill(client):\n    import os\n    return 1",
        "def skill(client):\n    from os import path\n    return 1",
        "def skill(client):\n    return open('x').read()",
        "def skill(client):\n    return eval('1+1')",
        "def skill(client):\n    return __import__('os')",
        "def skill(client):\n    return ().__class__.__bases__",
    ]
    for src in forbidden:
        try:
            compile_skill(src)
        except ValueError:
            continue
        raise AssertionError(f"sandbox accepted forbidden code:\n{src}")
    fn = compile_skill("def skill(client, items):\n    return sorted(items)")
    assert run_skill(fn, client=None, kwargs={"items": [3, 1, 2]}, timeout_s=2) == [1, 2, 3], \
        "clean skill did not run"


@check
def synthesis_pure_effectful_and_failure_paths():
    from praxis.models import Step
    from praxis.synthesizer import synthesize
    from tests.test_executor import FakeClient
    from tests.test_synthesizer import (
        EFFECTFUL_CODE, EFFECTFUL_CONTRACT, PURE_CODE, PURE_CONTRACT, StubLLM,
    )
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "s.db")
        try:
            # pure: in-memory test, skill registered
            gap = Step(seq=1, intent="group", operation="compute.group_by_label_and_render_table",
                       kind="compute", args={})
            res = synthesize(gap, FakeClient(), conn, StubLLM([PURE_CONTRACT, PURE_CODE]))
            assert res.ok and memory.get_skill(conn, gap.operation), "pure synthesis should register"
            # effectful: tested against the client, self-cleaned via the journal
            client = FakeClient()
            gap2 = Step(seq=1, intent="ensure label", operation="labels.ensure", kind="api", args={})
            res2 = synthesize(gap2, client, conn, StubLLM([EFFECTFUL_CONTRACT, EFFECTFUL_CODE]))
            assert res2.ok and client.undo_applied, "effectful synthesis should self-clean"
            # failure: 3 bad attempts, structured failure, nothing registered
            gap3 = Step(seq=1, intent="broken", operation="compute.broken", kind="compute", args={})
            bad = "def skill(client, issues):\n    import os\n    return os\n"
            res3 = synthesize(gap3, FakeClient(), conn, StubLLM([PURE_CONTRACT, bad, bad, bad]))
            assert not res3.ok and len(res3.attempts) == 3, "3-failure path should report cleanly"
            assert memory.get_skill(conn, "compute.broken") is None, "no skill on failure"
        finally:
            conn.close()


@check
def executor_learns_then_preapplies_precondition():
    # the Phase 3 headline in miniature, against ONE persistent repo (a single client) across
    # two runs — exactly the live gate's shape (fresh DB, same repo, twice):
    #   run 1: a bare add_label on a missing label 422s -> learn the precondition rule,
    #          synthesise+run labels.ensure (creates + caches the label), retry, succeed.
    #   run 2: the rule is pre-applied and the id is served from ref_cache -> zero NEW 422s.
    from praxis.executor import Executor
    from tests.test_executor import LabelAwareClient, ensure_label_synth
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "p.db")
        try:
            steps = [Step(seq=1, intent="c", operation="issues.create", kind="api", args={"title": "A"}),
                     Step(seq=2, intent="l", operation="issues.add_label", kind="api", args={"label": "priority:high"})]
            client = LabelAwareClient(existing_labels={"bug"})   # priority:high missing at first
            synth = ensure_label_synth(conn)
            r1 = Executor(conn, client, synthesizer=synth).run(run_id=1, steps=steps)
            rules = memory.rules_for(conn, "issues.add_label")
            assert any(rr["rule_type"] == "precondition" and rr["learned_in_run"] == 1 for rr in rules), \
                "run 1 must learn the issues.add_label precondition rule"
            assert any(s.operation == "issues.add_label" and s.status == "done" for s in r1), \
                "add_label must succeed on the retry after labels.ensure"
            assert memory.get_ref(conn, "label:priority:high") is not None, "the label id must be cached"

            client.label_422_count = 0                            # measure run 2 in isolation
            r2 = Executor(conn, client, synthesizer=synth).run(run_id=2, steps=steps)
            assert client.label_422_count == 0, "run 2 pre-applies the rule -> zero new 422s"
            assert not any(s.status == "failed" for s in r2), "run 2 takes no failure"
            ops = [s.operation for s in r2]
            assert ops.index("labels.ensure") < ops.index("issues.add_label"), "ensure injected first"
        finally:
            conn.close()


@check
def executor_learns_then_preapplies_milestone_precondition():
    # the LIVE Phase 3 headline's exact shape (the REAL GitHub constraint: a milestone must be
    # set by number, not title), against ONE persistent repo across two runs:
    #   run 1: set_milestone by title 422s -> learn the rule, synthesise+run milestones.ensure
    #          (creates the milestone, caches its number), retry with the number, succeed.
    #   run 2: the rule is pre-applied and the number served from ref_cache -> zero new 422s.
    from praxis.executor import Executor
    from tests.test_executor import MilestoneAwareClient, ensure_milestone_synth
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "m2.db")
        try:
            steps = [Step(seq=1, intent="c", operation="issues.create", kind="api", args={"title": "A"}),
                     Step(seq=2, intent="m", operation="issues.set_milestone", kind="api", args={"milestone": "Sprint 1"})]
            client = MilestoneAwareClient(existing_titles=())
            synth = ensure_milestone_synth(conn)
            r1 = Executor(conn, client, synthesizer=synth).run(run_id=1, steps=steps)
            rules = memory.rules_for(conn, "issues.set_milestone")
            assert any(rr["rule_type"] == "precondition" and rr["learned_in_run"] == 1 for rr in rules), \
                "run 1 must learn the issues.set_milestone precondition rule"
            assert any(s.operation == "issues.set_milestone" and s.status == "done" for s in r1), \
                "set_milestone must succeed on the retry with the resolved number"
            assert memory.get_ref(conn, "milestone:Sprint 1") is not None, "the number must be cached"

            client.milestone_422_count = 0                       # measure run 2 in isolation
            r2 = Executor(conn, client, synthesizer=synth).run(run_id=2, steps=steps)
            assert client.milestone_422_count == 0, "run 2 pre-applies + cached number -> zero new 422s"
            assert not any(s.status == "failed" for s in r2), "run 2 takes no failure"
            ops = [s.operation for s in r2]
            assert ops.index("milestones.ensure") < ops.index("issues.set_milestone"), "ensure injected first"
        finally:
            conn.close()


@check
def stale_cached_milestone_number_self_heals():
    # robustness (surfaced by a real run): a ref_cache milestone number can go stale when the
    # milestone is deleted on the platform. The cached id must not loop on 422 forever — on the
    # 422 the executor evicts the stale ref, re-resolves milestones.ensure against the live repo,
    # and the retry succeeds with the correct number.
    from praxis.executor import Executor
    from tests.test_executor import MilestoneAwareClient, ensure_milestone_synth
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "stale.db")
        try:
            memory.add_rule(conn, "issues.set_milestone", "precondition",
                            {"action": "milestones.ensure", "param": "milestone"}, learned_in_run=1)
            memory.put_ref(conn, "milestone:Sprint 1", "milestone", "2", run_id=1)  # STALE: #2 gone
            client = MilestoneAwareClient(existing_titles=("Sprint 1",))             # live == #1
            steps = [Step(seq=1, intent="c", operation="issues.create", kind="api", args={"title": "X"}),
                     Step(seq=2, intent="m", operation="issues.set_milestone", kind="api", args={"milestone": "Sprint 1"})]
            results = Executor(conn, client, synthesizer=ensure_milestone_synth(conn)).run(run_id=2, steps=steps)
            sm = [r for r in results if r.operation == "issues.set_milestone"]
            assert any(r.status == "done" for r in sm), "stale cached number must self-heal, not loop on 422"
            assert memory.get_ref(conn, "milestone:Sprint 1") == str(client.milestones["Sprint 1"]), \
                "the stale ref must be replaced with the live milestone number"
        finally:
            conn.close()


@check
def cross_instruction_milestone_transfer_via_fan_out():
    # THE Phase 4 headline, offline: a milestones.ensure precondition rule learned by an
    # Instruction-1 shape pre-applies on a DIFFERENT Instruction-3 fan-out shape's FIRST run
    # (rules are keyed by OPERATION, not instruction), and the shared 'Sprint 1' number is served
    # from ref_cache -> zero milestone-422s while every target issue lands on the milestone.
    from praxis.executor import Executor
    from tests.test_executor import TriageClient, ensure_milestone_synth
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "x.db")
        try:
            client = TriageClient()                  # one persistent repo across both instructions
            synth = ensure_milestone_synth(conn)
            # Instruction 1 shape: create + set a single milestone by title -> learns the rule,
            # creates + caches 'Sprint 1'.
            i1 = [Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "Login bug"}),
                  Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api", args={"milestone": "Sprint 1"})]
            Executor(conn, client, synthesizer=synth).run(run_id=1, steps=i1)
            assert any(r["learned_in_run"] == 1 for r in memory.rules_for(conn, "issues.set_milestone")), \
                "Instruction 1 must learn the set_milestone precondition rule"
            assert memory.get_ref(conn, "milestone:Sprint 1") is not None, "'Sprint 1' must be cached"

            # Instruction 3 shape (FIRST ever run): list + fan-out label + fan-out milestone.
            client.milestone_422_count = 0           # measure the transfer run in isolation
            i3 = [Step(seq=1, intent="list", operation="issues.list", kind="api", args={"filters": {"state": "open"}}),
                  Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "needs-triage", "target": "all"}),
                  Step(seq=3, intent="milestone", operation="issues.set_milestone", kind="api", args={"milestone": "Sprint 1", "target": "all"})]
            ex3 = Executor(conn, client, synthesizer=synth)
            r3 = ex3.run(run_id=2, steps=i3)
            assert client.milestone_422_count == 0, "transfer: zero milestone-422s on Instruction 3's first run"
            assert any(p["operation"] == "issues.set_milestone" and p["learned_in_run"] == 1
                       for p in ex3.preapplied_rules), "Instruction 3 must pre-apply the run-#1 rule"
            assert not any(s.status == "failed" for s in r3), "no failures on the warm transfer run"
            num = client.milestones["Sprint 1"]
            on_ms = sorted(i["number"] for i in client.issues if i["milestone"] == num)
            assert on_ms == [1, 2], "the fan-out set the shared milestone on every target issue"
        finally:
            conn.close()


@check
def identical_rerun_reuses_signature_and_plan_with_zero_llm():
    # the headline's plumbing: a second identical run reuses the exact-hash signature AND the
    # cached plan, so it makes ZERO LLM calls (deterministic reuse, Task 3.2).
    from praxis.config import Config
    from praxis.orchestrator import Orchestrator
    from tests.test_executor import FakeClient
    sig = {"verb": "create", "entity": "issue", "filters": {}, "artifact": "bug"}
    plan = {"steps": [{"seq": 1, "intent": "c", "operation": "issues.create",
                       "kind": "api", "args": {"title": "A"}}]}

    class SeqLLM:
        def __init__(self, payloads):
            self._p = list(payloads); self.llm_calls = 0; self.config = Config()
        def complete(self, messages, **kwargs):
            self.llm_calls += 1
            return self._p.pop(0)

    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "r.db")
        try:
            Orchestrator(conn, FakeClient(), SeqLLM([sig, plan])).run("create a bug issue")
            llm2 = SeqLLM([])  # no payloads: any recall/plan LLM call would raise
            Orchestrator(conn, FakeClient(), llm2).run("create a bug issue")
            assert llm2.llm_calls == 0, "an identical re-run must reuse signature + plan with no LLM"
        finally:
            conn.close()


@check
def reporter_renders():
    from praxis.reporter import build_report, render
    results = [StepResult(seq=1, operation="issues.create", status="done", latency_ms=5),
               StepResult(seq=2, operation="issues.add_label", status="failed", error="422")]
    rep = build_report("demo", results, api_calls=2, llm_calls=1, wall_ms=10,
                       memory_delta={"run_id": 1})
    text = render(rep)
    assert "issues.create" in text and "Metrics" in text, "render missing content"
    assert rep.status == "partial", "synthetic enrichment failure should be partial"


@check
def reporter_states_preapplied_rule():
    from praxis.reporter import build_report, render
    rep = build_report("demo", [StepResult(seq=1, operation="issues.add_label", status="done")],
                       api_calls=1, llm_calls=0, wall_ms=5,
                       memory_delta={"preapplied_rules": [
                           {"operation": "issues.add_label", "action": "labels.ensure",
                            "param": "label", "learned_in_run": 1}]})
    text = render(rep).lower()
    assert "pre-applied rule (learned run #1)" in text, "report must state the transferred rule"

@check
def compaction_trims_steps_and_preserves_runs_and_stats():
    # Phase 5: compaction trims the bulky per-step audit trail but keeps every `runs` row, so
    # the stats/curve learning ledger (which reads `runs`, not `run_steps`) is byte-identical.
    from praxis import metrics
    from praxis.compactor import compact
    sig = {"verb": "create", "entity": "issue", "filters": {}, "artifact": "bug"}
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "c.db")
        try:
            for _ in range(4):
                rid = memory.start_run(conn, "create a bug issue", sig)
                for s in (1, 2, 3):
                    memory.record_step(conn, rid, seq=s, intent="x", operation="issues.create",
                                       kind="api", status="done", latency_ms=5)
                    memory.bump_op_stats(conn, "issues.create", success=True, latency_ms=5)
                memory.finish_run(conn, rid, status="ok", api_calls=3, llm_calls=0,
                                  wall_ms=9, failure_count=0)
            before_steps = conn.execute("SELECT COUNT(*) c FROM run_steps").fetchone()["c"]
            before_stats = metrics.render_stats("i", metrics.runs_for_signature(conn, sig))
            before_ops = memory.all_op_stats(conn, "issues.create")[0]

            result = compact(conn, keep_recent=1)

            after_steps = conn.execute("SELECT COUNT(*) c FROM run_steps").fetchone()["c"]
            assert after_steps < before_steps, "compaction must trim run_steps"
            assert result["runs_before"] == result["runs_after"] == 4, "all runs retained"
            after_stats = metrics.render_stats("i", metrics.runs_for_signature(conn, sig))
            assert after_stats == before_stats, "stats unchanged for retained runs"
            assert memory.all_op_stats(conn, "issues.create")[0] == before_ops, \
                "op_stats is authoritative — compaction must not recompute it"
        finally:
            conn.close()


def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
