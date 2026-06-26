"""Seeding + the demo instruction set (plan Task 4.1).

`seed_repo` resets the sandbox repo to a known set of open, unassigned, mixed-label issues
so the live demo is repeatable — Instruction 2 groups them by label, Instruction 3 triages
them. It is **idempotent**: it matches existing open issues by title and creates only the
missing ones, so re-running it never duplicates.

`DEMO_INSTRUCTIONS` are the three instructions of increasing complexity (spec §11). They are
pinned here so `prewarm`, `stats`, and `curve` all refer to the exact same strings — the
signature/plan reuse and the cross-instruction transfer both hinge on that.
"""
from __future__ import annotations

from typing import Any

# The known sandbox state. Open + unassigned + mixed labels: bug x2, documentation,
# enhancement x2, question — enough variety for the group-by-label table, and five issues
# for the triage fan-out. Titles are stable + unique so title-matching keeps seed idempotent.
SEED_ISSUES: list[dict[str, Any]] = [
    {"title": "Crash when the search query is empty",
     "body": "Submitting an empty search 500s instead of returning no results.",
     "labels": ["bug"]},
    {"title": "Onboarding email has a broken link",
     "body": "The 'verify your account' link in the welcome email 404s.",
     "labels": ["documentation"]},
    {"title": "Add dark mode to the dashboard",
     "body": "Users have asked for a dark theme on the main dashboard.",
     "labels": ["enhancement"]},
    {"title": "How do I rotate my API token?",
     "body": "There's no documented way to rotate a token without downtime.",
     "labels": ["question"]},
    {"title": "Dashboard is slow with 10k rows",
     "body": "Rendering the reports table stalls for several seconds at scale.",
     "labels": ["bug", "enhancement"]},
]

# The three demo instructions (spec §11). Instruction 1 sets the 'Sprint 1' milestone — the
# precondition rule (issues.set_milestone -> milestones.ensure) is learned here. Instruction 3
# reuses that SAME milestone, so on its first ever run it gets both the rule transfer and a
# ref_cache hit on the already-resolved number.
DEMO_INSTRUCTION_1 = (
    "Create a high-priority bug issue titled 'Login times out after 30s' describing a login "
    "timeout, and put it on the 'Sprint 1' milestone"
)
DEMO_INSTRUCTION_2 = (
    "Find all open issues with no assignee, group them by label, and create a triage summary "
    "issue listing them"
)
DEMO_INSTRUCTION_3 = (
    "Add the label `needs-triage` and the 'Sprint 1' milestone to every open, unassigned, "
    "not-yet-triaged issue"
)
DEMO_INSTRUCTIONS = [DEMO_INSTRUCTION_1, DEMO_INSTRUCTION_2, DEMO_INSTRUCTION_3]


def _open_issues(client) -> list[dict[str, Any]]:
    """Open issues only (the seed's idempotency key space). GitHub's /issues list also
    returns PRs; we don't create those, so title-matching against it is safe here."""
    resp = client.rest_get(f"/repos/{client.repo}/issues", params={"state": "open"})
    return resp if isinstance(resp, list) else []


def seed_repo(client) -> dict[str, Any]:
    """Idempotently ensure SEED_ISSUES exist as open issues. Returns a summary of what was
    created vs already present. Labels are added directly (GitHub auto-creates a missing one)."""
    existing_titles = {i.get("title") for i in _open_issues(client)}
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    for spec in SEED_ISSUES:
        if spec["title"] in existing_titles:
            skipped.append(spec["title"])
            continue
        resp = client.rest_post(
            f"/repos/{client.repo}/issues",
            json={"title": spec["title"], "body": spec.get("body", "")},
        )
        number = resp.get("number") if isinstance(resp, dict) else None
        if number is not None and spec.get("labels"):
            client.rest_post(f"/repos/{client.repo}/issues/{number}/labels",
                             json={"labels": spec["labels"]})
        created.append({"number": number, "title": spec["title"], "labels": spec.get("labels", [])})
    return {"created": created, "skipped": skipped}


def prewarm(run_one, times: int, instructions: list[str] | None = None) -> list[dict[str, Any]]:
    """Run each demo instruction `times` times via the injected `run_one(instruction) -> Report`
    callable, populating memory (rules, refs, plans, op_stats) BEFORE the live demo call so the
    declining numbers are already persisted. Returns a per-run summary. The callable is injected
    so this is testable offline without touching the network."""
    instructions = instructions or DEMO_INSTRUCTIONS
    summary: list[dict[str, Any]] = []
    for round_no in range(1, times + 1):
        for idx, instruction in enumerate(instructions, start=1):
            report = run_one(instruction)
            summary.append({
                "round": round_no,
                "instruction_index": idx,
                "status": getattr(report, "status", None),
                "metrics": getattr(report, "metrics", {}),
            })
    return summary
