"""Orchestrator — the run lifecycle (spec §5): recall -> plan -> execute -> report ->
write-back. It owns the run row and the metric bookkeeping; the heavy lifting lives in
the components it wires together.
"""
from __future__ import annotations

import time

from . import memory, planner, recall
from .executor import Executor
from .models import Report
from .reporter import build_report


class Orchestrator:
    def __init__(self, db, client, llm, synthesizer=None):
        self.db = db
        self.client = client
        self.llm = llm
        self.synthesizer = synthesizer

    def run(self, instruction: str) -> Report:
        t0 = time.perf_counter()

        signature = recall.signature_for(self.db, instruction, self.llm)
        key = recall.signature_key(signature)
        run_id = memory.start_run(self.db, instruction, signature)

        had_cached_plan = memory.get_plan(self.db, key) is not None
        plan = planner.plan(instruction, signature, self.db, self.llm)

        executor = Executor(self.db, self.client, self.synthesizer)
        results = executor.run(run_id, plan.steps)

        wall_ms = int((time.perf_counter() - t0) * 1000)
        synthesized = [e["operation"] for e in executor.synthesis_events if e["ok"]]
        memory_delta = {
            "run_id": run_id,
            "plan_reused": had_cached_plan,
            "plan_stored": not had_cached_plan,
            "skills_synthesized": synthesized,
        }
        report = build_report(
            instruction, results,
            api_calls=self.client.api_calls,
            llm_calls=self.llm.llm_calls,
            wall_ms=wall_ms,
            memory_delta=memory_delta,
            synthesis_events=executor.synthesis_events,
        )

        memory.finish_run(self.db, run_id, report.status, report.metrics["api_calls"],
                          report.metrics["llm_calls"], wall_ms, report.metrics["failure_count"])
        if not had_cached_plan:
            memory.put_plan(self.db, key, plan, cost_api=self.client.api_calls,
                            cost_llm=self.llm.llm_calls, wall_ms=wall_ms, run_id=run_id)
        return report
