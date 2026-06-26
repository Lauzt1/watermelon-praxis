"""Seeding tests — the idempotent sandbox reset that gives Instructions 2/3 a known set of
open, unassigned, mixed-label issues to act on. Exercised offline against a fake repo that
tracks issues by title so the idempotency (no duplicates on re-run) is real."""
from praxis import seeding


class FakeRepo:
    """A minimal stand-in for the GitHub adapter that remembers created issues, so a second
    seed() call sees them as existing and creates nothing — the property seed must hold."""

    def __init__(self, repo="o/r"):
        self.repo = repo
        self.api_calls = 0
        self.journal = None
        self.issues = []          # {number, title, labels, assignee, state}
        self._n = 0

    def rest_get(self, path, params=None):
        self.api_calls += 1
        if path.endswith("/issues") and "/issues/" not in path:
            return [dict(i) for i in self.issues if i["state"] == "open"]
        return []

    def rest_post(self, path, json=None):
        self.api_calls += 1
        body = json or {}
        if path.endswith("/issues") and "/issues/" not in path:
            self._n += 1
            issue = {"number": self._n, "title": body.get("title"),
                     "labels": [], "assignee": None, "state": "open"}
            self.issues.append(issue)
            return {"number": self._n, "title": body.get("title")}
        if "/issues/" in path and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            for i in self.issues:
                if i["number"] == num:
                    i["labels"].extend({"name": n} for n in body.get("labels", []))
            return [{"name": n} for n in body.get("labels", [])]
        return {}

    def rest_patch(self, path, json=None):
        self.api_calls += 1
        return {}


def test_seed_creates_the_known_set_when_empty():
    client = FakeRepo()
    result = seeding.seed_repo(client)
    assert len(result["created"]) == len(seeding.SEED_ISSUES)
    titles = {i["title"] for i in client.issues}
    assert titles == {s["title"] for s in seeding.SEED_ISSUES}
    # every seeded issue is open + unassigned (what Instruction 3's filter selects)
    assert all(i["state"] == "open" and i["assignee"] is None for i in client.issues)
    # labels were applied so Instruction 2 can group by them
    assert all(i["labels"] for i in client.issues)


def test_seed_is_idempotent():
    client = FakeRepo()
    seeding.seed_repo(client)
    n_after_first = len(client.issues)
    result2 = seeding.seed_repo(client)
    assert result2["created"] == [], "a second seed must create nothing"
    assert len(client.issues) == n_after_first, "no duplicate issues on re-seed"


def test_demo_instructions_are_three_in_order():
    assert len(seeding.DEMO_INSTRUCTIONS) == 3
    # Instruction 1 sets the Sprint 1 milestone (where the precondition rule is learned);
    # Instruction 3 reuses that same milestone (shared ref-cache hit on transfer).
    assert "Sprint 1" in seeding.DEMO_INSTRUCTIONS[0]
    assert "Sprint 1" in seeding.DEMO_INSTRUCTIONS[2]
    assert "needs-triage" in seeding.DEMO_INSTRUCTIONS[2]
