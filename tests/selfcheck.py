"""Offline self-check: no network, no API keys. Exit non-zero on any failure."""
import sys, tempfile
from pathlib import Path
from praxis.db import connect
from praxis.models import Step, Plan, Report, StepResult

CHECKS = []
def check(fn): CHECKS.append(fn); return fn

@check
def schema_creates():
    with tempfile.TemporaryDirectory() as d:
        conn = connect(Path(d) / "t.db")
        try:
            n = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()  # release the file handle so Windows can clean up the tempdir
        assert {"runs", "skills", "learned_rules", "ref_cache"} <= n, "missing tables"

@check
def models_validate():
    p = Plan(signature="s", steps=[Step(seq=1, intent="x", operation="issues.create", kind="api", args={})])
    assert Plan.model_validate_json(p.model_dump_json()) == p

def main():
    failed = 0
    for fn in CHECKS:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} checks passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
