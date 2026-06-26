"""Executor tests — the spec §9 typed failure policy, exercised offline with a fake
client that mimics the real adapter: it counts calls, returns canned responses, and
auto-populates `journal` via the real `inverse_of` so rollback is end-to-end real.
"""
from praxis import memory
from praxis.executor import Executor
from praxis.models import Step
from praxis.platform.github import GitHubError, inverse_of


class FakeClient:
    """Behaves like praxis.platform.github.GitHub for the executor's purposes."""

    def __init__(self, fail_on_seq=None, repo="o/r"):
        self.repo = repo
        self.api_calls = 0
        self.journal = None          # executor sets a list per step; None disables journaling
        self.calls = []              # every mutating call: {op, path, body}
        self._fail_on_call = fail_on_seq
        self._n = 0
        self._issue_no = 0

    @property
    def undo_applied(self) -> bool:
        """True iff an inverse op was replayed (a delete, or a close PATCH)."""
        return any(
            c["op"] == "rest_delete" or (c["op"] == "rest_patch" and c["body"] == {"state": "closed"})
            for c in self.calls
        )

    def _mutate(self, op, path, body, resp):
        self.api_calls += 1
        self._n += 1
        self.calls.append({"op": op, "path": path, "body": body})
        if self._fail_on_call is not None and self._n == self._fail_on_call:
            raise GitHubError(422, "simulated failure", op, path)
        if self.journal is not None:
            inv = inverse_of(op, path, body, resp)
            if inv is not None:
                self.journal.append(inv)
        return resp

    def rest_post(self, path, json=None):
        body = json or {}
        if path.endswith("/issues"):
            self._issue_no += 1
            return self._mutate("rest_post", path, body, {"number": self._issue_no})
        if "/issues/" in path and path.endswith("/labels"):
            return self._mutate("rest_post", path, body, [{"name": n} for n in body.get("labels", [])])
        if path.endswith("/labels"):
            return self._mutate("rest_post", path, body, {"name": body.get("name")})
        if path.endswith("/milestones"):
            return self._mutate("rest_post", path, body, {"number": 1})
        return self._mutate("rest_post", path, body, {})

    def rest_patch(self, path, json=None):
        return self._mutate("rest_patch", path, json or {}, {})

    def rest_delete(self, path, json=None):
        return self._mutate("rest_delete", path, json or {}, {})

    def rest_get(self, path, params=None):
        self.api_calls += 1
        return []


def test_enrichment_failure_keeps_primary(db):
    client = FakeClient(fail_on_seq=2)
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"issue": 1, "label": "bug"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[0].status == "done"          # primary kept — NOT rolled back
    assert results[1].status == "failed"        # enrichment failure reported; run is partial
    assert not client.undo_applied, "primary must survive an enrichment failure"


def test_fatal_failure_rolls_back(db):
    client = FakeClient(fail_on_seq=2)
    steps = [
        Step(seq=1, intent="create A", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="create B", operation="issues.create", kind="api", args={"title": "B"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[0].status == "rolled_back"   # fatal -> prior mutation undone via journal
    assert results[1].status == "failed"
    assert client.undo_applied, "expected inverse op replayed"


def test_steps_after_fatal_are_skipped(db):
    client = FakeClient(fail_on_seq=1)          # the very first step (a create) fails
    steps = [
        Step(seq=1, intent="create A", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="create B", operation="issues.create", kind="api", args={"title": "B"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[0].status == "failed"
    assert results[1].status == "skipped"       # never ran after the fatal stop


def test_successful_run_records_steps_and_journal(db):
    client = FakeClient()
    steps = [Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "A"})]
    results = Executor(db, client).run(run_id=5, steps=steps)
    assert results[0].status == "done"
    # the create's inverse (close issue 1) is journaled for a possible later rollback
    entries = memory.journal_for(db, 5)
    assert len(entries) == 1 and entries[0]["inverse"].body == {"state": "closed"}
    row = db.execute("SELECT status FROM run_steps WHERE run_id=5 AND seq=1").fetchone()
    assert row["status"] == "done"
