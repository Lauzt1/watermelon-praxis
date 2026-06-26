"""Typed read/write helpers over the eight memory tables. Pure SQL, no LLM.

These are the only functions that touch the DB; every other module goes through here so
the memory shape stays in one place. Read helpers return plain dicts / pydantic models;
write helpers commit immediately so a crash mid-run still leaves durable, queryable state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .models import InverseOp, Plan, SkillContract, Step


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- runs / steps ---------------------------------------------------------------

def start_run(db: sqlite3.Connection, instruction: str, signature: dict) -> int:
    cur = db.execute(
        "INSERT INTO runs(instruction, signature_json, status, created_at) VALUES(?,?,?,?)",
        (instruction, json.dumps(signature), "running", _now()),
    )
    db.commit()
    return cur.lastrowid


def record_step(db: sqlite3.Connection, run_id: int, seq: int, intent: str, operation: str,
                kind: str, status: str, latency_ms: int = 0, error: str | None = None,
                resolution: str | None = None) -> None:
    db.execute(
        "INSERT INTO run_steps(run_id, seq, intent, operation, kind, status, latency_ms, error, resolution) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, seq, intent, operation, kind, status, latency_ms, error, resolution),
    )
    db.commit()


def finish_run(db: sqlite3.Connection, run_id: int, status: str, api_calls: int,
               llm_calls: int, wall_ms: int, failure_count: int) -> None:
    db.execute(
        "UPDATE runs SET status=?, api_calls=?, llm_calls=?, wall_ms=?, failure_count=? WHERE id=?",
        (status, api_calls, llm_calls, wall_ms, failure_count, run_id),
    )
    db.commit()


# --- plans ----------------------------------------------------------------------

def put_plan(db: sqlite3.Connection, signature: str, plan: Plan, cost_api: int = 0,
             cost_llm: int = 0, wall_ms: int = 0, run_id: int | None = None) -> None:
    steps_json = json.dumps([s.model_dump() for s in plan.steps])
    db.execute(
        "INSERT OR REPLACE INTO plans(signature, steps_json, cost_api, cost_llm, wall_ms, run_id, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (signature, steps_json, cost_api, cost_llm, wall_ms, run_id, _now()),
    )
    db.commit()


def get_plan(db: sqlite3.Connection, signature: str) -> Plan | None:
    row = db.execute("SELECT steps_json FROM plans WHERE signature=?", (signature,)).fetchone()
    if row is None:
        return None
    steps = [Step.model_validate(s) for s in json.loads(row["steps_json"])]
    return Plan(signature=signature, steps=steps)


# --- ref cache ------------------------------------------------------------------

def put_ref(db: sqlite3.Connection, key: str, kind: str, value: str,
            run_id: int | None = None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO ref_cache(key, kind, value, run_id, created_at) VALUES(?,?,?,?,?)",
        (key, kind, value, run_id, _now()),
    )
    db.commit()


def get_ref(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM ref_cache WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --- op_stats -------------------------------------------------------------------

def bump_op_stats(db: sqlite3.Connection, operation: str, success: bool, latency_ms: int) -> None:
    row = db.execute(
        "SELECT uses, successes, failures, avg_latency_ms FROM op_stats WHERE operation=?",
        (operation,),
    ).fetchone()
    if row is None:
        uses, successes, failures, avg = 0, 0, 0, 0.0
    else:
        uses, successes, failures, avg = row["uses"], row["successes"], row["failures"], row["avg_latency_ms"]
    new_uses = uses + 1
    new_avg = (avg * uses + latency_ms) / new_uses
    db.execute(
        "INSERT OR REPLACE INTO op_stats(operation, uses, successes, failures, avg_latency_ms, last_used) "
        "VALUES(?,?,?,?,?,?)",
        (operation, new_uses, successes + (1 if success else 0),
         failures + (0 if success else 1), new_avg, _now()),
    )
    db.commit()


# --- learned rules --------------------------------------------------------------

def add_rule(db: sqlite3.Connection, operation: str, rule_type: str, detail: dict,
             learned_in_run: int | None = None, confidence: float = 1.0) -> None:
    db.execute(
        "INSERT INTO learned_rules(operation, rule_type, detail_json, learned_in_run, confidence) "
        "VALUES(?,?,?,?,?)",
        (operation, rule_type, json.dumps(detail), learned_in_run, confidence),
    )
    db.commit()


def rules_for(db: sqlite3.Connection, operation: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id, operation, rule_type, detail_json, learned_in_run, confidence "
        "FROM learned_rules WHERE operation=? ORDER BY id",
        (operation,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "operation": r["operation"],
            "rule_type": r["rule_type"],
            "detail": json.loads(r["detail_json"]),
            "learned_in_run": r["learned_in_run"],
            "confidence": r["confidence"],
        }
        for r in rows
    ]


# --- inspection readers (the `memory` CLI before/after view) ---------------------

def all_rules(db: sqlite3.Connection, operation: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT operation, rule_type, detail_json, learned_in_run, confidence "
           "FROM learned_rules")
    params: tuple = ()
    if operation is not None:
        sql += " WHERE operation=?"
        params = (operation,)
    rows = db.execute(sql + " ORDER BY id", params).fetchall()
    return [
        {"operation": r["operation"], "rule_type": r["rule_type"],
         "detail": json.loads(r["detail_json"]), "learned_in_run": r["learned_in_run"],
         "confidence": r["confidence"]}
        for r in rows
    ]


def all_refs(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT key, kind, value, run_id FROM ref_cache ORDER BY key"
    ).fetchall()
    return [{"key": r["key"], "kind": r["kind"], "value": r["value"], "run_id": r["run_id"]}
            for r in rows]


def all_op_stats(db: sqlite3.Connection, operation: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT operation, uses, successes, failures, avg_latency_ms, last_used "
           "FROM op_stats")
    params: tuple = ()
    if operation is not None:
        sql += " WHERE operation=?"
        params = (operation,)
    rows = db.execute(sql + " ORDER BY operation", params).fetchall()
    return [
        {"operation": r["operation"], "uses": r["uses"], "successes": r["successes"],
         "failures": r["failures"], "avg_latency_ms": r["avg_latency_ms"], "last_used": r["last_used"]}
        for r in rows
    ]


def all_skills(db: sqlite3.Connection, name: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT name, status, version, uses, successes, failures FROM skills"
    params: tuple = ()
    if name is not None:
        sql += " WHERE name=?"
        params = (name,)
    rows = db.execute(sql + " ORDER BY name", params).fetchall()
    return [
        {"name": r["name"], "status": r["status"], "version": r["version"],
         "uses": r["uses"], "successes": r["successes"], "failures": r["failures"]}
        for r in rows
    ]


# --- skills ---------------------------------------------------------------------

def put_skill(db: sqlite3.Connection, name: str, contract: SkillContract | dict, code: str,
              status: str = "active", version: int = 1) -> None:
    contract_json = contract.model_dump_json() if isinstance(contract, SkillContract) else json.dumps(contract)
    db.execute(
        "INSERT OR REPLACE INTO skills(name, contract_json, code, status, version, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (name, contract_json, code, status, version, _now()),
    )
    db.commit()


def get_skill(db: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT name, contract_json, code, status, version, uses, successes, failures FROM skills WHERE name=?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "contract": json.loads(row["contract_json"]),
        "code": row["code"],
        "status": row["status"],
        "version": row["version"],
        "uses": row["uses"],
        "successes": row["successes"],
        "failures": row["failures"],
    }


# --- undo journal ---------------------------------------------------------------

def journal_append(db: sqlite3.Connection, run_id: int, seq: int, inverse: InverseOp) -> None:
    db.execute(
        "INSERT INTO undo_journal(run_id, seq, inverse_op_json, applied) VALUES(?,?,?,0)",
        (run_id, seq, inverse.model_dump_json()),
    )
    db.commit()


def journal_for(db: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    """Forward (insertion) order; the executor replays it in reverse to roll back."""
    rows = db.execute(
        "SELECT id, seq, inverse_op_json, applied FROM undo_journal WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "seq": r["seq"],
            "inverse": InverseOp.model_validate_json(r["inverse_op_json"]),
            "applied": r["applied"],
        }
        for r in rows
    ]


def mark_applied(db: sqlite3.Connection, journal_id: int) -> None:
    db.execute("UPDATE undo_journal SET applied=1 WHERE id=?", (journal_id,))
    db.commit()
