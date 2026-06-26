"""Reporter — turns step results into the structured Report the brief requires.

Derives the run status from the per-step outcomes (spec §9):
  * ok      — every step done;
  * partial — an enrichment step failed (or a dependent was skipped) but the primary
              artifact survived;
  * failed  — a fatal failure occurred (a non-enrichment op failed, triggering rollback).
The Report is both human-rendered and JSON-serialisable (`--json`).
"""
from __future__ import annotations

from typing import Any

from . import operations
from .models import Report, StepResult


def _derive_status(results: list[StepResult]) -> str:
    statuses = {r.status for r in results}
    if "rolled_back" in statuses:
        return "failed"
    if any(r.status == "failed" and not operations.is_enrichment(r.operation) for r in results):
        return "failed"
    if statuses - {"done"}:
        return "partial"
    return "ok"


def build_report(instruction: str, results: list[StepResult], *, api_calls: int,
                 llm_calls: int, wall_ms: int, memory_delta: dict[str, Any],
                 synthesis_events: list[dict[str, Any]] | None = None) -> Report:
    failure_count = sum(1 for r in results if r.status == "failed")
    metrics = {
        "api_calls": api_calls,
        "llm_calls": llm_calls,
        "wall_ms": wall_ms,
        "failure_count": failure_count,
    }
    return Report(
        instruction=instruction,
        status=_derive_status(results),
        steps=results,
        metrics=metrics,
        memory_delta=memory_delta or {},
        synthesis_events=synthesis_events or [],
    )


def render(report: Report) -> str:
    lines = [
        f"Instruction: {report.instruction}",
        f"Status: {report.status.upper()}",
        "",
        "Steps:",
    ]
    for s in report.steps:
        line = f"  [{s.status:<10}] {s.seq}. {s.operation}"
        if s.error:
            line += f"  - error: {s.error}"
        if s.resolution:
            line += f"  ({s.resolution})"
        lines.append(line)
    m = report.metrics
    lines += [
        "",
        f"Metrics: api_calls={m.get('api_calls')} llm_calls={m.get('llm_calls')} "
        f"wall_ms={m.get('wall_ms')} failure_count={m.get('failure_count')}",
    ]
    if report.memory_delta:
        lines.append("Memory delta: " + " ".join(f"{k}={v}" for k, v in report.memory_delta.items()))
    if report.synthesis_events:
        lines.append(f"Synthesis: {len(report.synthesis_events)} event(s)")
        for ev in report.synthesis_events:
            lines.append(f"  - {ev}")
    return "\n".join(lines)
