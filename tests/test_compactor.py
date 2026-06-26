"""Phase 5, Task 5.1 — memory compaction.

The invariant under test: `stats`/`curve` read from `runs` (metrics.runs_for_signature),
never from `run_steps`; and `op_stats` is maintained incrementally on every op
(memory.bump_op_stats). So trimming old `run_steps` provably cannot change either output.
compact() trims step detail for old runs while retaining every `runs` row.
"""
from praxis import memory, metrics
from praxis.compactor import compact


def _seed_runs(db, n_runs, steps_per_run=3):
    """n_runs runs sharing ONE signature (same instruction re-run), each with steps and
    incrementally-bumped op_stats — the realistic shape compaction acts on."""
    sig = {"verb": "create", "entity": "issue", "filters": {}, "artifact": "bug"}
    run_ids = []
    for i in range(n_runs):
        rid = memory.start_run(db, "create a bug issue", sig)
        for s in range(1, steps_per_run + 1):
            memory.record_step(db, rid, seq=s, intent="make it", operation="issues.create",
                               kind="api", status="done", latency_ms=5)
            memory.bump_op_stats(db, "issues.create", success=True, latency_ms=5)
        memory.finish_run(db, rid, status="ok", api_calls=steps_per_run, llm_calls=0,
                          wall_ms=10 + i, failure_count=0)
        run_ids.append(rid)
    return sig, run_ids


def _count(db, table):
    return db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def test_compact_trims_old_steps_keeps_runs_and_stats(db):
    sig, _ = _seed_runs(db, n_runs=5, steps_per_run=3)
    before_steps = _count(db, "run_steps")        # 5 * 3 = 15
    before_runs = _count(db, "runs")              # 5
    before_stats = metrics.render_stats("create a bug issue",
                                        metrics.runs_for_signature(db, sig))

    result = compact(db, keep_recent=2)

    # run_steps dropped; every runs row retained
    assert _count(db, "run_steps") == 6, "3 oldest runs * 3 steps trimmed, 2 recent kept"
    assert _count(db, "runs") == before_runs, "compaction must retain all runs rows"
    # stats output is byte-identical — it reads runs, not run_steps
    after_stats = metrics.render_stats("create a bug issue",
                                       metrics.runs_for_signature(db, sig))
    assert after_stats == before_stats, "stats must be unchanged for retained runs"
    assert result["run_steps_before"] == before_steps
    assert result["run_steps_after"] == 6
    assert result["run_steps_trimmed"] == 9
    assert result["runs_compacted"] == 3
    assert result["runs_before"] == result["runs_after"] == before_runs


def test_compact_preserves_op_stats(db):
    _seed_runs(db, n_runs=4, steps_per_run=2)
    before = memory.all_op_stats(db, "issues.create")[0]
    compact(db, keep_recent=1)
    after = memory.all_op_stats(db, "issues.create")[0]
    assert after == before, "op_stats is authoritative — compaction must not recompute it"


def test_compact_is_idempotent(db):
    _seed_runs(db, n_runs=4, steps_per_run=2)
    compact(db, keep_recent=2)
    mid = _count(db, "run_steps")
    compact(db, keep_recent=2)
    assert _count(db, "run_steps") == mid, "re-compacting keeps the recent window stable"


def test_compact_preserves_plan_shape_for_compacted_runs(db):
    sig, _ = _seed_runs(db, n_runs=3, steps_per_run=2)
    from praxis.recall import signature_key
    plan_key = signature_key(sig)
    assert memory.get_plan(db, plan_key) is None, "no plan stored yet"

    result = compact(db, keep_recent=1)

    plan = memory.get_plan(db, plan_key)
    assert plan is not None, "a compacted run's plan shape must survive in `plans`"
    assert len(plan.steps) == 2, "the reconstructed plan preserves the op sequence"
    # the 3 compacted runs share one signature, so only one plan row is written
    assert result["plans_preserved"] == 1


def test_compact_keep_recent_zero_trims_everything(db):
    _seed_runs(db, n_runs=3, steps_per_run=2)
    compact(db, keep_recent=0)
    assert _count(db, "run_steps") == 0, "keep_recent=0 trims all step detail"
    assert _count(db, "runs") == 3, "runs are still retained"
