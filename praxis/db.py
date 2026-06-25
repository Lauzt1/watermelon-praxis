import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instruction TEXT NOT NULL,
  signature_json TEXT NOT NULL,
  status TEXT NOT NULL,
  api_calls INTEGER NOT NULL DEFAULT 0,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  wall_ms INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  intent TEXT,
  operation TEXT,
  kind TEXT,
  status TEXT,
  latency_ms INTEGER,
  error TEXT,
  resolution TEXT
);
CREATE TABLE IF NOT EXISTS op_stats (
  operation TEXT PRIMARY KEY,
  uses INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  avg_latency_ms REAL NOT NULL DEFAULT 0,
  last_used TEXT
);
CREATE TABLE IF NOT EXISTS plans (
  signature TEXT PRIMARY KEY,
  steps_json TEXT NOT NULL,
  cost_api INTEGER NOT NULL DEFAULT 0,
  cost_llm INTEGER NOT NULL DEFAULT 0,
  wall_ms INTEGER NOT NULL DEFAULT 0,
  run_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_cache (
  key TEXT PRIMARY KEY,
  kind TEXT,
  value TEXT,
  run_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS undo_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  inverse_op_json TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skills (
  name TEXT PRIMARY KEY,
  contract_json TEXT NOT NULL,
  code TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1,
  uses INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation TEXT NOT NULL,
  rule_type TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  learned_in_run INTEGER,
  confidence REAL NOT NULL DEFAULT 1.0
);
"""

def connect(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
