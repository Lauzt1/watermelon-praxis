"""Metrics tests — the run-1-vs-N table and the learning-curve export, driven off a DB of
hand-inserted runs so the signature filtering, the delta, and the CSV/SVG output are all
verified offline (no network)."""
from praxis import memory, metrics


def _run(db, sig, api, llm, wall, fail, status="ok"):
    rid = memory.start_run(db, "demo instruction", sig)
    memory.finish_run(db, rid, status, api, llm, wall, fail)
    return rid


SIG = {"verb": "create", "entity": "issue", "filters": {}, "artifact": "bug"}
OTHER = {"verb": "list", "entity": "issue", "filters": {"state": "open"}, "artifact": ""}


def test_runs_for_signature_filters_and_numbers(db):
    _run(db, SIG, 10, 4, 70000, 1, "partial")
    _run(db, OTHER, 3, 1, 2000, 0)             # different signature -> excluded
    _run(db, SIG, 4, 0, 3000, 0, "ok")
    rows = metrics.runs_for_signature(db, SIG)
    assert [r["run_number"] for r in rows] == [1, 2]
    assert [r["api_calls"] for r in rows] == [10, 4]
    assert rows[0]["status"] == "partial" and rows[1]["status"] == "ok"


def test_runs_for_signature_empty_when_no_match(db):
    assert metrics.runs_for_signature(db, {"verb": "x", "entity": "y", "filters": {}, "artifact": ""}) == []


def test_render_stats_shows_run1_vs_latest_delta(db):
    _run(db, SIG, 10, 4, 70000, 1, "partial")
    _run(db, SIG, 4, 0, 3000, 0, "ok")
    rows = metrics.runs_for_signature(db, SIG)
    text = metrics.render_stats("demo", rows)
    assert "Run 1 vs latest" in text
    assert "10 -> 4" in text                   # api_calls decline rendered
    assert "-6" in text                        # the delta


def test_write_curve_emits_csv_and_svg(db, tmp_path):
    _run(db, SIG, 10, 4, 70000, 1, "partial")
    _run(db, SIG, 4, 0, 3000, 0, "ok")
    rows = metrics.runs_for_signature(db, SIG)
    out = metrics.write_curve(rows, SIG, out_dir=tmp_path)
    csv_text = (tmp_path / f"learning_{out['slug']}.csv").read_text(encoding="utf-8")
    assert "api_calls" in csv_text and "10" in csv_text and "4" in csv_text
    svg_text = (tmp_path / f"learning_{out['slug']}.svg").read_text(encoding="utf-8")
    assert "<svg" in svg_text and "polyline" in svg_text
