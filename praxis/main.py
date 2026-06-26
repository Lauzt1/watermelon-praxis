"""Praxis CLI. Phase 1 wires `run` and `doctor`; later phases add seed/prewarm/memory/
stats/curve/compact/rollback.
"""
from __future__ import annotations

import argparse
import sys

from . import memory
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


def render_memory(db, operation: str | None = None) -> str:
    """Format the persistent memory for the `memory` command — the before/after view the
    brief asks for: learned rules, the resolved-id cache, per-operation stats, and the
    synthesised-skill registry. Pure reader, so it is testable offline."""
    head = "Praxis memory" + (f" - operation {operation}" if operation else "")
    lines = [head]

    lines.append("\nlearned_rules:")
    rules = memory.all_rules(db, operation)
    if not rules:
        lines.append("  (none)")
    for r in rules:
        lines.append(f"  [{r['operation']}] {r['rule_type']}: {r['detail']} "
                     f"(learned in run #{r['learned_in_run']}, confidence {r['confidence']})")

    lines.append("\nref_cache:")
    refs = memory.all_refs(db)
    if not refs:
        lines.append("  (none)")
    for r in refs:
        lines.append(f"  {r['key']} = {r['value']}  ({r['kind']}, run #{r['run_id']})")

    lines.append("\nop_stats:")
    stats = memory.all_op_stats(db, operation)
    if not stats:
        lines.append("  (none)")
    for s in stats:
        lines.append(f"  {s['operation']}: uses={s['uses']} ok={s['successes']} "
                     f"fail={s['failures']} avg_ms={s['avg_latency_ms']:.0f}")

    lines.append("\nskills:")
    skills = memory.all_skills(db, operation)
    if not skills:
        lines.append("  (none)")
    for s in skills:
        lines.append(f"  {s['name']} [{s['status']} v{s['version']}] "
                     f"uses={s['uses']} ok={s['successes']} fail={s['failures']}")
    return "\n".join(lines)


def cmd_memory(args: argparse.Namespace, config: Config) -> None:
    conn = connect(config.db_path)
    try:
        print(render_memory(conn, getattr(args, "operation", None)))
    finally:
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

    p_mem = sub.add_parser("memory", help="inspect learned rules, ref-cache, op-stats, skills")
    p_mem.add_argument("--operation", help="filter rules/op-stats/skills to one operation")

    args = parser.parse_args(argv)
    if args.command == "run":
        cmd_run(args, config)
    elif args.command == "doctor":
        cmd_doctor(args, config)
    elif args.command == "memory":
        cmd_memory(args, config)


if __name__ == "__main__":
    main()
