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

# The triage marker Instruction 3 applies. A canonical issue carrying it (or a milestone) is a
# leftover from a prior triage run, so seed resets it to keep each demo run starting un-triaged.
TRIAGE_LABEL = "needs-triage"


def _open_issues(client) -> list[dict[str, Any]]:
    """Open issues only (the seed's reset key space). GitHub's /issues list also returns PRs;
    they carry a 'pull_request' key, so we skip them and never touch a PR."""
    resp = client.rest_get(f"/repos/{client.repo}/issues", params={"state": "open"})
    issues = resp if isinstance(resp, list) else []
    return [i for i in issues if "pull_request" not in i]


def _label_names(issue: dict) -> set[str]:
    return {l.get("name") for l in (issue.get("labels") or []) if isinstance(l, dict)}


def _is_clean_canonical(issue: dict, canonical_titles: set[str]) -> bool:
    """A canonical-titled issue that is open, un-triaged (no needs-triage label) and on no
    milestone — i.e. reusable as-is, so seed keeps it instead of churning a new one."""
    return (issue.get("title") in canonical_titles
            and TRIAGE_LABEL not in _label_names(issue)
            and not issue.get("milestone"))


def seed_repo(client, reset: bool = True) -> dict[str, Any]:
    """Converge the repo to exactly the canonical SEED_ISSUES as fresh, open, un-triaged issues
    (spec §11 'resets the sandbox to a known set'). Idempotent: when the repo is already clean it
    is a no-op; otherwise it closes every other open issue (stale or dirty) and creates the
    missing canonical ones. Labels are added directly (GitHub auto-creates a missing one).

    `reset=False` keeps the older create-only behaviour (used by tooling that must not close)."""
    canonical_titles = {s["title"] for s in SEED_ISSUES}
    reusable: dict[str, dict] = {}
    closed: list[dict[str, Any]] = []
    for issue in _open_issues(client):
        if reset and _is_clean_canonical(issue, canonical_titles) and issue["title"] not in reusable:
            reusable[issue["title"]] = issue        # keep one clean copy per canonical title
        elif reset:
            client.rest_patch(f"/repos/{client.repo}/issues/{issue['number']}",
                              json={"state": "closed"})
            closed.append({"number": issue["number"], "title": issue.get("title")})
        elif issue["title"] in canonical_titles:
            reusable.setdefault(issue["title"], issue)

    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    for spec in SEED_ISSUES:
        if spec["title"] in reusable:
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
    return {"created": created, "skipped": skipped, "closed": closed}


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
