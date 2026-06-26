"""Reporter tests — status derivation, metrics, human render, and JSON validity."""
import json

from praxis.models import StepResult
from praxis.reporter import build_report, render


def _results_partial():
    return [
        StepResult(seq=1, operation="issues.create", status="done", latency_ms=10),
        StepResult(seq=2, operation="issues.add_label", status="failed", error="422 label missing",
                   resolution="enrichment op failed; primary kept, continuing"),
    ]


def test_build_report_metrics_and_partial_status():
    rep = build_report("inst", _results_partial(), api_calls=2, llm_calls=1, wall_ms=50,
                       memory_delta={"refs_added": 1})
    assert rep.status == "partial"          # an enrichment step failed, primary intact
    assert rep.metrics["api_calls"] == 2 and rep.metrics["failure_count"] == 1
    assert rep.memory_delta == {"refs_added": 1}


def test_status_ok_when_all_done():
    results = [StepResult(seq=1, operation="issues.create", status="done")]
    assert build_report("i", results, api_calls=1, llm_calls=1, wall_ms=1, memory_delta={}).status == "ok"


def test_status_failed_when_rolled_back():
    results = [StepResult(seq=1, operation="issues.create", status="rolled_back"),
               StepResult(seq=2, operation="issues.create", status="failed", error="boom")]
    assert build_report("i", results, api_calls=2, llm_calls=1, wall_ms=1, memory_delta={}).status == "failed"


def test_status_failed_when_non_enrichment_fails_first():
    results = [StepResult(seq=1, operation="issues.create", status="failed", error="boom"),
               StepResult(seq=2, operation="issues.add_label", status="skipped")]
    assert build_report("i", results, api_calls=1, llm_calls=1, wall_ms=1, memory_delta={}).status == "failed"


def test_render_contains_step_statuses_and_metrics():
    text = render(build_report("inst", _results_partial(), api_calls=2, llm_calls=1, wall_ms=50,
                               memory_delta={"refs_added": 1}))
    assert "issues.create" in text and "done" in text and "failed" in text
    assert "api_calls" in text and "PARTIAL" in text


def test_render_is_ascii_safe():
    # the CLI prints to consoles that may be cp1252 (Windows); render must not emit
    # characters that can't be encoded there.
    text = render(build_report("inst", _results_partial(), api_calls=2, llm_calls=1, wall_ms=50,
                               memory_delta={"refs_added": 1}))
    text.encode("ascii")  # raises UnicodeEncodeError if a non-ASCII char slipped in


def test_report_json_is_valid():
    rep = build_report("inst", _results_partial(), api_calls=2, llm_calls=1, wall_ms=50,
                       memory_delta={"refs_added": 1})
    parsed = json.loads(rep.model_dump_json())
    assert parsed["status"] == "partial" and parsed["metrics"]["failure_count"] == 1
