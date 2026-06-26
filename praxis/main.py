"""Praxis CLI. Phase 1 wires `run` and `doctor`; later phases add seed/prewarm/memory/
stats/curve/compact/rollback.
"""
from __future__ import annotations

import argparse
import sys

from .config import Config, load
from .db import connect
from .llm import LLM
from .orchestrator import Orchestrator
from .platform.github import GitHub
from .reporter import render
from .synthesizer import synthesize

REQUIRED_TABLES = {"runs", "run_steps", "op_stats", "plans", "ref_cache",
                   "undo_journal", "skills", "learned_rules"}


def cmd_run(args: argparse.Namespace, config: Config) -> None:
    conn = connect(config.db_path)
    client = GitHub(config.github_token, config.github_repo)
    llm = LLM(config)
    # Bridge the executor's self.synthesizer(step, run_refs) call to the canonical
    # synthesize(gap, client, db, llm, run_refs); the closure binds the live client/db/llm.
    synthesizer = lambda step, refs=None: synthesize(step, client, conn, llm, run_refs=refs)  # noqa: E731
    try:
        report = Orchestrator(conn, client, llm, synthesizer=synthesizer).run(args.instruction)
        print(report.model_dump_json(indent=2) if args.json else render(report))
    finally:
        client.close()
        conn.close()


def cmd_doctor(args: argparse.Namespace, config: Config) -> None:
    ok = True

    for name, val in [("OPENROUTER_API_KEY", config.openrouter_key),
                      ("GITHUB_TOKEN", config.github_token),
                      ("GITHUB_REPO", config.github_repo)]:
        present = bool(val)
        ok &= present
        print(f"[{'PASS' if present else 'FAIL'}] config {name} {'present' if present else 'MISSING'}")

    try:
        conn = connect(config.db_path)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        schema_ok = REQUIRED_TABLES <= names
        print(f"[{'PASS' if schema_ok else 'FAIL'}] db schema at {config.db_path}")
    except Exception as e:  # noqa: BLE001
        schema_ok = False
        print(f"[FAIL] db error: {e}")
    ok &= schema_ok

    client = GitHub(config.github_token, config.github_repo)
    try:
        user = client.rest_get("/user")
        print(f"[PASS] github /user -> {user.get('login')}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] github /user: {e}")
    try:
        repo = client.rest_get(f"/repos/{config.github_repo}")
        print(f"[PASS] repo access -> {repo.get('full_name')} "
              f"(issues={'on' if repo.get('has_issues') else 'OFF'})")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[FAIL] repo access: {e}")
    finally:
        client.close()

    print(f"\ndoctor: {'all green' if ok else 'problems found'}")
    sys.exit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    config = load()
    parser = argparse.ArgumentParser(prog="praxis", description="Autonomous GitHub agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a natural-language instruction")
    p_run.add_argument("instruction", help="the instruction in quotes")
    p_run.add_argument("--json", action="store_true", help="emit the report as JSON")

    sub.add_parser("doctor", help="check keys, DB/schema, and GitHub access")

    args = parser.parse_args(argv)
    if args.command == "run":
        cmd_run(args, config)
    elif args.command == "doctor":
        cmd_doctor(args, config)


if __name__ == "__main__":
    main()
