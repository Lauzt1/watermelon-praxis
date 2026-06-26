"""Metrics (plan Task 4.2) — the measurable self-learning signal, read straight from `runs`.

`runs_for_signature` selects the runs of one instruction (matched by its canonical signature,
so equivalent phrasings collapse together) and numbers them 1..N in execution order.
`render_stats` prints the run-1-vs-latest table; `write_curve` exports `learning_<sig>.csv`
plus a dependency-free hand-rolled SVG line chart of api_calls per run. All pure read +
string building, so it is fully testable offline.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

_METRIC_KEYS = ("api_calls", "llm_calls", "wall_ms", "failure_count")


def runs_for_signature(db, signature: dict) -> list[dict[str, Any]]:
    """Every recorded run whose signature equals `signature` (order-independent dict compare),
    numbered 1..N by insertion order — the run-over-run learning ledger for one instruction."""
    rows = db.execute(
        "SELECT id, instruction, signature_json, status, api_calls, llm_calls, wall_ms, "
        "failure_count, created_at FROM runs ORDER BY id"
    ).fetchall()
    out: list[dict[str, Any]] = []
    n = 0
    for r in rows:
        try:
            sig = json.loads(r["signature_json"])
        except (TypeError, ValueError):
            continue
        if sig != signature:
            continue
        n += 1
        out.append({
            "run_number": n,
            "run_id": r["id"],
            "status": r["status"],
            "api_calls": r["api_calls"],
            "llm_calls": r["llm_calls"],
            "wall_ms": r["wall_ms"],
            "failure_count": r["failure_count"],
        })
    return out


def render_stats(instruction: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"No runs recorded for: {instruction}"
    lines = [f"Stats for: {instruction}",
             f"({len(rows)} run(s), matched by signature)", ""]
    header = (f"  {'run':>3}  {'run_id':>6}  {'status':<8}  {'api':>4}  {'llm':>4}  "
              f"{'wall_ms':>8}  {'fail':>4}")
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for r in rows:
        lines.append(f"  {r['run_number']:>3}  {r['run_id']:>6}  {r['status']:<8}  "
                     f"{r['api_calls']:>4}  {r['llm_calls']:>4}  {r['wall_ms']:>8}  "
                     f"{r['failure_count']:>4}")
    if len(rows) > 1:
        first, last = rows[0], rows[-1]

        def delta(k: str) -> str:
            d = last[k] - first[k]
            return f"{first[k]} -> {last[k]} ({'+' if d > 0 else ''}{d})"

        lines += ["", "Run 1 vs latest:"]
        lines += [f"  {k + ':':<15}{delta(k)}" for k in _METRIC_KEYS]
    return "\n".join(lines)


def _sig_slug(signature: dict) -> str:
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def write_curve(rows: list[dict[str, Any]], signature: dict, out_dir="data",
                metric: str = "api_calls") -> dict[str, str]:
    """Export `learning_<sig>.csv` + a hand-rolled SVG line chart of `metric` per run."""
    slug = _sig_slug(signature)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"learning_{slug}.csv"
    svg_path = out / f"learning_{slug}.svg"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run_number", "run_id", "status", *_METRIC_KEYS])
        for r in rows:
            w.writerow([r["run_number"], r["run_id"], r["status"],
                        *(r[k] for k in _METRIC_KEYS)])

    svg_path.write_text(_svg_line_chart(rows, metric), encoding="utf-8")
    return {"csv": str(csv_path), "svg": str(svg_path), "slug": slug}


def _svg_line_chart(rows: list[dict[str, Any]], metric: str) -> str:
    """A minimal, dependency-free SVG: axes, a polyline of `metric` per run, point markers and
    value labels. Kept deliberately simple — readable source we can explain on camera."""
    W, H, pad = 520, 300, 48
    ys = [r[metric] for r in rows] or [0]
    n = len(rows)
    ymax = max(ys + [1])

    def px(i: int) -> float:
        return pad if n <= 1 else pad + (W - 2 * pad) * i / (n - 1)

    def py(v: float) -> float:
        return H - pad - (H - 2 * pad) * v / ymax

    points = " ".join(f"{px(i):.1f},{py(y):.1f}" for i, y in enumerate(ys))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="sans-serif">',
        f'<rect width="{W}" height="{H}" fill="white"/>',
        f'<text x="{W/2:.0f}" y="24" text-anchor="middle" font-size="16">'
        f'Learning curve — {metric} per run</text>',
        # axes
        f'<line x1="{pad}" y1="{H-pad}" x2="{W-pad}" y2="{H-pad}" stroke="#888"/>',
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{H-pad}" stroke="#888"/>',
        f'<text x="{pad-8}" y="{py(ymax):.1f}" text-anchor="end" font-size="11">{ymax}</text>',
        f'<text x="{pad-8}" y="{H-pad:.1f}" text-anchor="end" font-size="11">0</text>',
    ]
    if n > 1:
        parts.append(f'<polyline fill="none" stroke="#2b7" stroke-width="2.5" points="{points}"/>')
    for i, y in enumerate(ys):
        cx, cy = px(i), py(y)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="#2b7"/>')
        parts.append(f'<text x="{cx:.1f}" y="{cy-10:.1f}" text-anchor="middle" font-size="11">{y}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{H-pad+18:.0f}" text-anchor="middle" '
                     f'font-size="11">run {rows[i]["run_number"]}</text>')
    parts.append("</svg>")
    return "\n".join(parts)
