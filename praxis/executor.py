"""Executor — runs typed steps against the platform, with the spec §9 failure policy.

Two dispatch classes (spec §5, "Dispatch class — Option A"):
  * single-endpoint ops bind directly to ONE primitive via a deterministic table;
  * compound / compute.* ops resolve through the skills registry, synthesising on first
    use (the Synthesizer arrives in Phase 2; until then a missing skill is an error).

Every successful mutation's inverse (produced by the adapter into `client.journal`) is
persisted to `undo_journal`, so a fatal failure can be auto-rolled-back by replaying it
in reverse. Failure policy: an *enrichment* op failing is non-fatal (keep the primary,
continue, run is `partial`); any other failure is fatal (stop, roll back, run is `failed`).
"""
from __future__ import annotations

import time
from typing import Any

from . import memory, operations, sandbox
from .models import Step, StepResult

# A synthesised skill is in-process and pure-ish; if it overruns this it's a runaway, not
# slow I/O (effectful skills make a handful of fast calls). The watchdog turns that into a
# clean failure instead of a hang.
SKILL_TIMEOUT_S = 20.0


class ExecutorError(Exception):
    """Raised for an operation the executor cannot dispatch (unknown, or a compound op
    with no registered skill and no synthesizer yet)."""


class Executor:
    def __init__(self, db, client, synthesizer=None):
        self.db = db
        self.client = client
        self.synthesizer = synthesizer
        # Within-run references (e.g. the issue number a create produced, or the issue list
        # a compute step consumes). The planner can't know runtime values, so later steps
        # resolve them from here. Transient run state, distinct from the persistent
        # ref_cache (label/milestone ids) wired in Phase 3.
        self.run_refs: dict[str, Any] = {}
        # Synthesis events recorded this run, surfaced by the reporter.
        self.synthesis_events: list[dict[str, Any]] = []

    # --- dispatch ---------------------------------------------------------------

    def _issue_target(self, args: dict) -> Any:
        # Accept only an integer-like explicit issue; anything else (a missing key, or a
        # planner placeholder such as "ctx:issue_number_from_step1") resolves from the
        # within-run context populated by the preceding create.
        raw = args.get("issue", args.get("issue_number"))
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        issue = self.run_refs.get("last_issue")
        if issue is None:
            raise ExecutorError("no target issue (none usable in args and none created this run)")
        return issue

    def _dispatch(self, step: Step) -> Any:
        op, args, repo = step.operation, step.args, self.client.repo

        if op == "issues.create":
            body = {k: args[k] for k in ("title", "body", "assignees", "labels", "milestone") if k in args}
            body = self._thread_compute_result(body)
            resp = self.client.rest_post(f"/repos/{repo}/issues", json=body)
            if isinstance(resp, dict) and resp.get("number") is not None:
                self.run_refs["last_issue"] = resp["number"]
            return resp
        if op == "issues.add_label":
            issue = self._issue_target(args)
            labels = args.get("labels") or [args["label"]]
            return self.client.rest_post(f"/repos/{repo}/issues/{issue}/labels", json={"labels": labels})
        if op == "issues.set_milestone":
            issue = self._issue_target(args)
            return self.client.rest_patch(f"/repos/{repo}/issues/{issue}", json={"milestone": args["milestone"]})
        if op == "issues.list":
            resp = self.client.rest_get(f"/repos/{repo}/issues", params=args.get("filters") or args or None)
            self.run_refs["issues"] = resp        # thread the list to a downstream compute step
            return resp

        if operations.is_compound(op):
            result = self._dispatch_skill(step)
            self.run_refs[op] = result            # e.g. run_refs["compute.group_by_label..."]
            if operations.is_compute(op):
                self.run_refs["result"] = result  # friendly alias for the latest transform output
            return result

        raise ExecutorError(f"unknown operation {op!r}")

    def _thread_compute_result(self, body: dict) -> dict:
        """If a compute transform produced text this run, fold it into the issue body. The
        planner can't embed a runtime-computed table, so the executor appends it (after any
        prose the planner wrote) — this is what makes the triage issue actually list them."""
        result = self.run_refs.get("result")
        if not isinstance(result, str) or not result.strip():
            return body
        existing = body.get("body") or ""
        if result in existing:
            return body
        body = dict(body)
        body["body"] = (existing.rstrip() + "\n\n" + result).strip() if existing else result
        return body

    def _dispatch_skill(self, step: Step) -> Any:
        """Look up the skill; synthesise it on first use; then compile + run it sandboxed."""
        skill = memory.get_skill(self.db, step.operation)
        if skill is None:
            if self.synthesizer is None:
                raise ExecutorError(
                    f"no registered skill for {step.operation!r} and no synthesizer"
                )
            result = self.synthesizer(step)       # reason -> build -> test -> register
            self.synthesis_events.append({
                "operation": step.operation,
                "ok": bool(getattr(result, "ok", False)),
                "attempts": len(getattr(result, "attempts", []) or []),
            })
            if not getattr(result, "ok", False):
                raise ExecutorError(
                    f"synthesis failed for {step.operation!r} after "
                    f"{len(getattr(result, 'attempts', []) or [])} attempts"
                )
            skill = memory.get_skill(self.db, step.operation)
            if skill is None:
                raise ExecutorError(f"skill {step.operation!r} not registered after synthesis")

        fn = sandbox.compile_skill(skill["code"])
        kwargs = self._skill_kwargs(step, skill["contract"])
        return sandbox.run_skill(fn, client=self.client, kwargs=kwargs, timeout_s=SKILL_TIMEOUT_S)

    def _skill_kwargs(self, step: Step, contract: dict | None) -> dict[str, Any]:
        """Build the skill's kwargs: planner-supplied literals first, then fill any declared
        input from this run's references (e.g. the `issues` a compute transform consumes)."""
        inputs = (contract or {}).get("inputs", {}) if isinstance(contract, dict) else {}
        kwargs: dict[str, Any] = dict(step.args or {})
        for name in inputs:
            if name in kwargs:
                continue
            if name in self.run_refs:
                kwargs[name] = self.run_refs[name]
            elif name in ("issues", "items", "rows", "data", "results") and "issues" in self.run_refs:
                kwargs[name] = self.run_refs["issues"]
        return kwargs

    # --- run loop ---------------------------------------------------------------

    def run(self, run_id: int, steps: list[Step]) -> list[StepResult]:
        results: list[StepResult] = []
        completed_mutations: list[StepResult] = []  # done mutations, flipped on rollback
        fatal = False
        self.run_refs = {}          # fresh within-run reference scope
        self.synthesis_events = []   # fresh per-run synthesis log

        for step in steps:
            if fatal:
                break
            t0 = time.perf_counter()
            self.client.journal = []
            try:
                self._dispatch(step)
                latency = int((time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001 — any failure is classified, not swallowed
                latency = int((time.perf_counter() - t0) * 1000)
                memory.bump_op_stats(self.db, step.operation, success=False, latency_ms=latency)
                if operations.is_enrichment(step.operation):
                    results.append(StepResult(
                        seq=step.seq, operation=step.operation, status="failed",
                        latency_ms=latency, error=str(e),
                        resolution="enrichment op failed; primary kept, continuing",
                    ))
                    continue
                results.append(StepResult(
                    seq=step.seq, operation=step.operation, status="failed",
                    latency_ms=latency, error=str(e),
                    resolution="fatal failure; rolled back this run's completed mutations",
                ))
                self._rollback(run_id)
                for cr in completed_mutations:
                    cr.status = "rolled_back"
                fatal = True
                continue

            memory.bump_op_stats(self.db, step.operation, success=True, latency_ms=latency)
            inverses = list(self.client.journal or [])
            for inv in inverses:
                memory.journal_append(self.db, run_id, step.seq, inv)
            res = StepResult(seq=step.seq, operation=step.operation, status="done", latency_ms=latency)
            results.append(res)
            if inverses:
                completed_mutations.append(res)

        # steps that never ran after a fatal stop are reported as skipped
        seen = {r.seq for r in results}
        for step in steps:
            if step.seq not in seen:
                results.append(StepResult(seq=step.seq, operation=step.operation, status="skipped"))
        results.sort(key=lambda r: r.seq)

        # persist per-step evidence with final statuses
        by_seq = {r.seq: r for r in results}
        for step in steps:
            r = by_seq[step.seq]
            memory.record_step(self.db, run_id, step.seq, step.intent, step.operation, step.kind,
                               r.status, r.latency_ms, r.error, r.resolution)
        return results

    # --- rollback ---------------------------------------------------------------

    def _rollback(self, run_id: int) -> None:
        """Replay this run's recorded inverses in reverse; do not journal the undo ops."""
        self.client.journal = None
        for entry in reversed(memory.journal_for(self.db, run_id)):
            inv = entry["inverse"]
            self._apply_inverse(inv)
            memory.mark_applied(self.db, entry["id"])

    def _apply_inverse(self, inv) -> None:
        if inv.method == "rest_patch":
            self.client.rest_patch(inv.path, json=inv.body)
        elif inv.method == "rest_delete":
            self.client.rest_delete(inv.path, json=inv.body or None)
        elif inv.method == "rest_post":
            self.client.rest_post(inv.path, json=inv.body)
