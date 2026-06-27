# Praxis — Autonomous Platform Intelligence Agent (GitHub)

Praxis turns a natural-language instruction into **real GitHub API execution**, backed by persistent
structured memory that changes its decisions across runs, **runtime capability synthesis** (it writes
and tests new tool code on the fly), and a **measurable self-learning loop** (api/llm/wall/failure
counts that decline as memory is used). No agent framework, no vector DB — the LLM is the brain;
deterministic Python + SQLite are the hands and the memory.

- **Architecture** (the brief's three questions, with real numbers): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- **Demo** (the three instructions, expected behaviour, exact commands): [`docs/DEMO.md`](docs/DEMO.md)

## Setup

Requires Python 3.11+ (developed on 3.12). Two separate repos: this repo is the agent's source; a
**throwaway sandbox repo** (`GITHUB_REPO`) is the platform it operates on — all API calls hit that.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows; use .venv/bin/python on POSIX
cp .env.example .env                                # then set the three required keys below
```

The editable install registers a **`praxis`** command, so you can run `praxis doctor`,
`praxis seed`, `praxis run "..."` directly (use `.venv/Scripts/praxis ...`, or just
`praxis ...` once the venv is activated). The explicit `python -m praxis.main ...` form
below works identically and needs no activation.

`.env` keys (see `.env.example`) — the first three are **required**; the rest have working defaults:

```dotenv
OPENROUTER_API_KEY=sk-or-...           # required — OpenRouter key (LLM access)
GITHUB_TOKEN=ghp_...                   # required — PAT with repo scope on the sandbox repo
GITHUB_REPO=your-name/praxis-sandbox   # required — the disposable target repo
PRAXIS_MODEL_WORKHORSE=deepseek/deepseek-v4-pro     # optional — synthesis (reasoning model)
PRAXIS_MODEL_PLANNER=deepseek/deepseek-v4-flash     # optional — planning + recall (reasoning model)
PRAXIS_DB=data/praxis.db               # optional — created at runtime; gitignored
```

## Single-command run

```bash
.venv/Scripts/python -m praxis.main run "Create a high-priority bug issue titled 'Login times out after 30s' describing a login timeout, and put it on the 'Sprint 1' milestone"
```

That makes real GitHub calls and prints a structured report (per-step status + metrics + memory
delta). Add `--json` for machine output, or `--no-learning` for the zero-memory cold baseline.

## Commands

| command | what it does |
|---|---|
| `run "<instruction>"` | run a natural-language instruction (`--json`, `--no-learning`) |
| `doctor` | check keys, DB/schema, live GitHub `/user` + repo access |
| `seed` | idempotently reset the sandbox repo to the known demo issues |
| `prewarm --times N` | run the three demo instructions N rounds to populate memory |
| `memory [--operation OP]` | inspect learned rules, ref-cache, op-stats, and the skill registry |
| `stats "<instruction>"` | run-1-vs-N metrics table for an instruction (matched by signature) |
| `curve "<instruction>"` | export a learning-curve CSV + SVG to `data/` |
| `compact [--keep-recent N]` | trim old `run_steps` into aggregates; keep `runs`/`op_stats` authoritative |

## Tests

Fully offline, no keys, no network:

```bash
.venv/Scripts/python -m pytest -q          # unit + integration suite
.venv/Scripts/python -m tests.selfcheck    # brief-aligned self-check harness (exit 0 = green)
```

`doctor` and the demo are the only commands that touch the network.
