"""Offline self-check: no network, no API keys. Exit non-zero on any failure."""
import sys, tempfile
from pathlib import Path
from praxis.db import connect
from praxis.models import Step, Plan, Report, StepResult
from praxis.platform.github import inverse_of

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

@check
def inverse_of_roundtrips_five_shapes():
    # create issue -> close
    inv = inverse_of("rest_post", "/repos/o/r/issues", {}, {"number": 42})
    assert inv.method == "rest_patch" and inv.path == "/repos/o/r/issues/42" and inv.body == {"state": "closed"}
    # add label -> remove label
    inv = inverse_of("rest_post", "/repos/o/r/issues/42/labels", {"labels": ["bug"]}, [{"name": "bug"}])
    assert inv.method == "rest_delete" and inv.path == "/repos/o/r/issues/42/labels/bug"
    # set milestone -> clear milestone
    inv = inverse_of("rest_patch", "/repos/o/r/issues/42", {"milestone": 5}, {"number": 42})
    assert inv.method == "rest_patch" and inv.body == {"milestone": None}
    # create label -> delete label
    inv = inverse_of("rest_post", "/repos/o/r/labels", {"name": "priority:high"}, {"name": "priority:high"})
    assert inv.method == "rest_delete" and inv.path == "/repos/o/r/labels/priority:high"
    # create milestone -> delete milestone
    inv = inverse_of("rest_post", "/repos/o/r/milestones", {"title": "Q3"}, {"number": 7})
    assert inv.method == "rest_delete" and inv.path == "/repos/o/r/milestones/7"
    # reads / unknown mutations -> None
    assert inverse_of("rest_get", "/repos/o/r/issues", None, [{"number": 1}]) is None
    assert inverse_of("rest_patch", "/repos/o/r/issues/42", {"title": "x"}, {"number": 42}) is None

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
