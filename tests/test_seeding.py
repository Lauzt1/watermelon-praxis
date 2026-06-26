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
            return [{"number": i["number"], "title": i["title"],
                     "labels": [{"name": n} for n in i["labels"]],
                     "assignee": i["assignee"],
                     "milestone": ({"number": i["milestone"]} if i["milestone"] else None),
                     "state": i["state"]}
                    for i in self.issues if i["state"] == "open"]
        return []

    def rest_post(self, path, json=None):
        self.api_calls += 1
        body = json or {}
        if path.endswith("/issues") and "/issues/" not in path:
            self._n += 1
            issue = {"number": self._n, "title": body.get("title"),
                     "labels": [], "assignee": None, "milestone": None, "state": "open"}
            self.issues.append(issue)
            return {"number": self._n, "title": body.get("title")}
        if "/issues/" in path and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            for i in self.issues:
                if i["number"] == num:
                    i["labels"].extend(n for n in body.get("labels", []))
            return [{"name": n} for n in body.get("labels", [])]
        return {}

    def rest_patch(self, path, json=None):
        self.api_calls += 1
        body = json or {}
        num = int(path.split("/issues/")[1].split("/")[0])
        for i in self.issues:
            if i["number"] == num:
                i.update(body)
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


def test_seed_is_idempotent_when_already_clean():
    client = FakeRepo()
    seeding.seed_repo(client)
    n_after_first = len([i for i in client.issues if i["state"] == "open"])
    result2 = seeding.seed_repo(client)
    assert result2["created"] == [], "a second seed must create nothing when already clean"
    assert result2["closed"] == [], "clean canonical issues are kept, not churned"
    open_now = [i for i in client.issues if i["state"] == "open"]
    assert len(open_now) == n_after_first, "no duplicate / no extra issues on re-seed"


def test_seed_closes_stale_non_canonical_issues():
    client = FakeRepo()
    # a stray open issue not in the canonical set (e.g. a leftover from an earlier phase)
    client.issues.append({"number": 99, "title": "Leftover duplicate",
                          "labels": [], "assignee": None, "milestone": None, "state": "open"})
    result = seeding.seed_repo(client)
    assert any(c["number"] == 99 for c in result["closed"]), "the stale issue must be closed"
    stale = next(i for i in client.issues if i["number"] == 99)
    assert stale["state"] == "closed"
    open_titles = {i["title"] for i in client.issues if i["state"] == "open"}
    assert open_titles == {s["title"] for s in seeding.SEED_ISSUES}, "ends at exactly the canonical set"


def test_seed_resets_a_dirty_canonical_issue():
    # a canonical-titled issue left in a triaged state (needs-triage label + a milestone) from a
    # prior run must be closed and recreated fresh, so each demo run starts un-triaged.
    client = FakeRepo()
    spec = seeding.SEED_ISSUES[0]
    client.issues.append({"number": 50, "title": spec["title"],
                          "labels": ["bug", "needs-triage"], "assignee": None,
                          "milestone": 2, "state": "open"})
    result = seeding.seed_repo(client)
    assert any(c["number"] == 50 for c in result["closed"]), "the dirty canonical issue is reset"
    fresh = [i for i in client.issues
             if i["title"] == spec["title"] and i["state"] == "open"]
    assert len(fresh) == 1 and "needs-triage" not in fresh[0]["labels"], "recreated fresh, un-triaged"
    assert fresh[0]["milestone"] is None


def test_demo_instructions_are_three_in_order():
    assert len(seeding.DEMO_INSTRUCTIONS) == 3
    # Instruction 1 sets the Sprint 1 milestone (where the precondition rule is learned);
    # Instruction 3 reuses that same milestone (shared ref-cache hit on transfer).
    assert "Sprint 1" in seeding.DEMO_INSTRUCTIONS[0]
    assert "Sprint 1" in seeding.DEMO_INSTRUCTIONS[2]
    assert "needs-triage" in seeding.DEMO_INSTRUCTIONS[2]
