"""Memory compaction (plan Phase 5, Task 5.1) — the measured nice-to-have.

Why this is safe *by construction*: `stats`/`curve` read from `runs`
(see metrics.runs_for_signature), never from `run_steps`; and `op_stats` is maintained
incrementally on every op (memory.bump_op_stats). So the per-run metric ledger AND the
per-operation aggregates are both authoritative *without* the step-level detail. Compaction
therefore trims old `run_steps` (the bulky, per-step audit trail) while keeping every
`runs` row — the run-over-run learning numbers are provably unchanged.

It deliberately does NOT recompute `op_stats` (re-summing the rows being deleted would
double-count an already-authoritative aggregate). Before trimming, it preserves each
compacted run's executed plan *shape* in `plans` so the audit trail survives the deletion.
"""
from __future__ import annotations

import json
import sqlite3

from . import memory
from .models import Plan, Step


def _runs_to_compact(db: sqlite3.Connection, keep_recent: int) -> list[int]:
    """Run ids eligible for trimming: everything but the most recent `keep_recent` runs."""
    ids = [r["id"] for r in db.execute("SELECT id FROM runs ORDER BY id")]
    if keep_recent <= 0:
        return ids
    return ids[:-keep_recent] if keep_recent < len(ids) else []


def _preserve_plan(db: sqlite3.Connection, run_id: int) -> bool:
    """Reconstruct a run's executed operation sequence from its steps and store it under the
    run's signature key — but only if no (richer) plan is already cached there.

    `run_steps` carry no `args`, so the reconstructed plan preserves the op/kind *sequence*
    (the auditable shape), not arguments; the orchestrator's stored plan (with args) is always
    kept in preference. Returns True iff a new plan row was written.
    """
    row = db.execute("SELECT signature_json FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return False
    signature = json.loads(row["signature_json"])
    key = json.dumps(signature, sort_keys=True)
    if memory.get_plan(db, key) is not None:
        return False
    steps = db.execute(
        "SELECT seq, intent, operation, kind FROM run_steps WHERE run_id=? ORDER BY seq",
        (run_id,),
    ).fetchall()
    if not steps:
        return False
    plan = Plan(signature=key, steps=[
        Step(seq=s["seq"], intent=s["intent"] or "", operation=s["operation"] or "",
             kind=s["kind"] if s["kind"] in ("api", "compute") else "api", args={})
        for s in steps
    ])
    memory.put_plan(db, key, plan, run_id=run_id)
    return True


def compact(db: sqlite3.Connection, keep_recent: int = 50) -> dict:
    """Trim `run_steps` for all but the most recent `keep_recent` runs.

    Returns a before/after summary (row counts, runs compacted, plans preserved) — the numbers
    the `compact` CLI prints for the demo. `runs`, `op_stats` and `plans` are never reduced.
    """
    def count(table: str) -> int:
        return db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    run_steps_before = count("run_steps")
    runs_before = count("runs")

    old_ids = [rid for rid in _runs_to_compact(db, keep_recent)
               if db.execute("SELECT 1 FROM run_steps WHERE run_id=? LIMIT 1",
                             (rid,)).fetchone()]

    plans_preserved = 0
    for rid in old_ids:
        if _preserve_plan(db, rid):
            plans_preserved += 1
    if old_ids:
        db.executemany("DELETE FROM run_steps WHERE run_id=?", [(rid,) for rid in old_ids])
        db.commit()

    run_steps_after = count("run_steps")
    return {
        "runs_before": runs_before,
        "runs_after": count("runs"),                       # invariant: == runs_before
        "run_steps_before": run_steps_before,
        "run_steps_after": run_steps_after,
        "run_steps_trimmed": run_steps_before - run_steps_after,
        "runs_compacted": len(old_ids),
        "plans_preserved": plans_preserved,
        "keep_recent": keep_recent,
    }
