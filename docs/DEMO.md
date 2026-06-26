# Praxis — Demo

Three instructions of increasing complexity, run live against the throwaway sandbox repo
(`GITHUB_REPO` in `.env`). Each `run` makes **real** GitHub API calls — the change appears on
github.com as it happens. The exact instruction strings live in `praxis/seeding.py`
(`DEMO_INSTRUCTIONS`) so `prewarm`, `stats`, and `curve` all key off the same signatures.

## Setup (once, before the demo)

```bash
.venv/Scripts/python -m praxis.main doctor          # keys + DB schema + GitHub access all green
.venv/Scripts/python -m praxis.main seed            # idempotently reset the sandbox to 5 known issues
.venv/Scripts/python -m praxis.main prewarm --times 5   # warm the learning memory (rules/refs/plans/op_stats)
```

`seed` converges the repo to exactly five open, unassigned, mixed-label issues (bug ×2,
documentation, enhancement ×2, question) and closes anything stale — so the run is repeatable.
`prewarm` runs the three instructions ahead of time so the declining numbers are already persisted;
the showcase skill is still synthesised **live** in the demo to prove synthesis is real.

> Run `seed` again right before the live demo so the repo starts un-triaged (an earlier run may have
> left the `needs-triage` label / `Sprint 1` milestone on the issues).

## Instruction 1 — Simple (NL → exec + structured report; teaches the rule)

```bash
.venv/Scripts/python -m praxis.main run "Create a high-priority bug issue titled 'Login times out after 30s' describing a login timeout, and put it on the 'Sprint 1' milestone"
```

- **Cold (fresh memory):** creates the issue, adds the `high-priority`/`bug` labels (GitHub
  auto-creates a missing label, no 422), then `set_milestone 'Sprint 1'` by title returns a **real
  HTTP 422** (a milestone must be set by number). The agent learns the precondition rule
  `issues.set_milestone → milestones.ensure`, **synthesises `milestones.ensure` live**, creates +
  caches the `Sprint 1` number, retries OK. Report: `PARTIAL` → then OK on retry.
- **Warm (after prewarm):** signature + plan reused (0 planner LLM), `milestones.ensure` injected up
  front and served from `ref_cache` (0 resolve API), `set_milestone` uses the cached number → **0
  422s**. Recorded curve, same instruction twice: api 10→4, llm 4→0, wall 70.8s→3.2s, fail 1→0.

## Instruction 2 — Compound (decomposition + live synthesis + partial-failure)

```bash
.venv/Scripts/python -m praxis.main run "Find all open issues with no assignee, group them by label, and create a triage summary issue listing them"
```

- Lists open/unassigned issues, then needs a transform that has no single GitHub endpoint, so it
  **synthesises `compute.group_by_label_and_render_table`** (pure transform, tested in-memory on the
  run's real issue data), and posts a triage summary issue containing a real group-by-label markdown
  table. The report carries the synthesis event; re-running reuses the skill with **0** synthesis
  LLM calls.

## Instruction 3 — Synthesis + transfer (THE headline)

```bash
.venv/Scripts/python -m praxis.main run "Add the label \`needs-triage\` and the 'Sprint 1' milestone to every open, unassigned, not-yet-triaged issue"
```

- Fans out over every open/unassigned/untriaged issue, adding the label and the `Sprint 1`
  milestone to each. On its **first ever run** it pre-applies the rule Instruction 1 learned in run
  #1 (*"Pre-applied rule (learned run #1): issues.set_milestone → milestones.ensure"*),
  `skills_synthesized=[]` (skill reused), `Sprint 1` served from `ref_cache` → **0 milestone-422s**.
- **Cross-instruction transfer, WARM vs COLD (two fresh DBs, same repo):** api 17→12, llm 4→2, wall
  70.1s→36.5s, **failures 1→0**, synthesis: live → none. A `--no-learning` run is the zero-memory
  contrast (the 422 is surfaced, milestone set on nobody — no silent half-completion).

## Inspect the learning

```bash
.venv/Scripts/python -m praxis.main memory --operation issues.set_milestone   # rule + cached number + op_stats
.venv/Scripts/python -m praxis.main stats "Add the label \`needs-triage\` and the 'Sprint 1' milestone to every open, unassigned, not-yet-triaged issue"
.venv/Scripts/python -m praxis.main curve "Add the label \`needs-triage\` and the 'Sprint 1' milestone to every open, unassigned, not-yet-triaged issue"
.venv/Scripts/python -m praxis.main compact            # row-count + recall-latency before/after (the nice-to-have)
```

`stats` prints the run-1-vs-latest table; `curve` writes `data/learning_<sig>.csv` + `.svg`;
`compact` trims old `run_steps` into the aggregates while retaining every `runs` row (recorded at
scale: 900 → 30 run_steps, recall ~14× faster, the learning ledger byte-identical).
