# Praxis — Architecture

Praxis turns a natural-language instruction into real GitHub API execution, backed by persistent
structured memory that changes its decisions, runtime capability synthesis, and a measurable
self-learning loop. The LLM is the brain (decompose, reason, write tool code at runtime);
deterministic Python + SQLite are the hands and the memory. This page answers the brief's three
questions, with real pre-warm numbers.

## 1. What does memory store, and why relational rather than vector?

Memory is eight single-purpose SQLite tables in `data/praxis.db`, split into two layers.

**Execution memory** — `runs` (the learning-number ledger: api/llm/wall/failure per run),
`run_steps` (per-step audit trail, trimmable by compaction), `op_stats` (per-operation
uses/successes/failures/latency), `plans` (the cheapest winning decomposition per signature, reused
for free), `ref_cache` (resolve-once: `milestone:Sprint 1 → 4`), and `undo_journal` (inverse ops
backing rollback and synthesis self-clean).

**Capability memory** — `skills` (synthesised tools persisted as code + contract) and
`learned_rules` (constraints discovered the hard way, **read before execution**).

It is relational, not vector, because the agent must make a *different decision*, not retrieve
similar text. "`issues.set_milestone` needs the milestone to exist as a number first → ensure it
now and skip the 422" is an exact fact keyed by **operation**; a similarity search can't return a
precondition. Joins on `operation` are precisely what carry learning across instructions.

## 2. How does runtime capability synthesis work?

The executor knows only how to call a primitive or look up a skill — it contains **no** hand-written
compound operation. When it hits a compound or `compute.*` step with no `skills` row, the
Synthesizer (`synthesizer.py`) runs a five-step loop:

1. **Reason a contract** (workhorse LLM) — name, typed inputs/output, the subset of GitHub
   primitives it may call, and safe `test_args`; Pydantic-validated.
2. **Generate** a single `skill(client, **kwargs)` composing only those primitives.
3. **Compile in a restricted sandbox** (`sandbox.py`) — an AST walk rejects
   `import`/`open`/`eval`/`socket`/dunder access; whitelisted builtins; soft timeout.
4. **Test by kind** — a pure `compute.*` transform is tested in-memory on the run's *real* upstream
   data (so shape bugs are caught); an effectful skill (`milestones.ensure`) is tested against the
   real API, its inverse ops replayed from the journal to self-clean.
5. **Register** to `skills` (code + contract), or after **N=3** attempts report each error — never a
   fake success.

Instruction 2's `compute.group_by_label_and_render_table` is the on-camera proof of pure synthesis;
`milestones.ensure` is the effectful one.

Synthesised skills are not trusted forever: every dispatch updates the skill's success rate, and a
skill whose `confidence = successes/uses` falls below 0.5 (after ≥3 uses) is **quarantined and
re-synthesised with a version bump** on its next use — the agent repairs its own tools rather than
running stale code (platform drift, or a synthesis that rots against changed data).

## 3. What's the learning signal, and what changes on run N vs run 1?

The signal is **api_calls, llm_calls, wall_ms, failure_count per run**, read live from `runs`.
Improvement is emergent from memory being *used*: constraint pre-loading (inject the prerequisite
before the 422), ref caching (resolve an id once), skill reuse (synthesise once), plan reuse.

**Same instruction, twice (fresh DB):** Run 1 PARTIAL — api 10, llm 4, wall 70.8s, 1 failure (eats a
real milestone-422, learns the rule, synthesises `milestones.ensure`). Run 2 OK — **api 4, llm 0,
wall 3.2s, 0 failures**.

**The headline — cross-instruction transfer.** Rules are keyed by *operation*
(`issues.set_milestone → milestones.ensure`), so the rule Instruction 1 learns in run #1 pre-applies
on **Instruction 3's first ever run**. WARM vs COLD (two fresh DBs, same repo): api 17→12, llm 4→2,
wall 70.1s→36.5s, **failures 1→0**, zero synthesis (skill reused), milestone served from
`ref_cache`. The crispest signals are **llm 4→0** (synthesis + planning eliminated) and **fail 1→0**
(the 422 eliminated). Honest framing: for create-heavy work the API floor is the irreducible
mutation — the decline kills *overhead* calls (id resolution, the 422 retry, re-planning), not the
create itself. Compaction keeps recall cheap at scale: 900 run_steps → 30, recall 1.68 ms →
0.12 ms (~14×), with the `runs` ledger and `op_stats` untouched. A second, capability-layer signal
comes from skill self-healing: a degraded skill's confidence recovers after a rebuild (e.g. 0.33 →
quarantined → 1.0) and the healed run's `failure_count` returns to 0 — learning on the *tools*, not
just the per-run cost ledger.

## What I'd build next

- **Multi-agent decomposition** (planner → specialist executors) for failure isolation + parallelism.
- **Harder sandbox isolation** (subprocess + rlimits, vs the current in-process namespace).
- **Deeper synthesis verification** — an explicit `verify(client, result)` post-condition per skill.
- **Optional embedding recall layer**, off by default, only if the corpus outgrows
  exact-signature matching.
- **Eager in-run self-healing** (rebuild + retry within the same run, vs the shipped lazy
  across-runs heal) and **rule-confidence decay** on contradiction.
