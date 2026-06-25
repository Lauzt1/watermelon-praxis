from praxis.db import connect, SCHEMA_SQL

EXPECTED_TABLES = {
    "runs", "run_steps", "op_stats", "plans", "ref_cache",
    "undo_journal", "skills", "learned_rules",
}

def test_schema_creates_all_eight_tables(tmp_path):
    conn = connect(tmp_path / "t.db")
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert EXPECTED_TABLES <= names

def test_schema_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    connect(p).close()
    connect(p).close()  # second connect must not raise
