"""Executor tests — the spec §9 typed failure policy, exercised offline with a fake
client that mimics the real adapter: it counts calls, returns canned responses, and
auto-populates `journal` via the real `inverse_of` so rollback is end-to-end real.
"""
from types import SimpleNamespace

from praxis import memory
from praxis.executor import Executor
from praxis.models import SkillContract, Step
from praxis.platform.github import GitHubError, inverse_of


# The synthesised labels.ensure skill the Phase 3 tests register: resolve-or-create a label
# using only allowed primitives + whitelisted builtins (no try/except — `Exception` isn't a
# whitelisted builtin in the sandbox, so it lists-and-checks instead).
ENSURE_LABEL_CODE = (
    "def skill(client, label):\n"
    "    existing = client.rest_get('/repos/' + client.repo + '/labels')\n"
    "    for lab in existing:\n"
    "        if lab['name'] == label:\n"
    "            return lab\n"
    "    return client.rest_post('/repos/' + client.repo + '/labels', json={'name': label})\n"
)


def ensure_label_synth(db):
    """A stub synthesizer that registers the labels.ensure skill (no real LLM/API), so the
    executor's constraint-retry can drive a real resolve-or-create against the fake client."""
    contract = SkillContract(name="labels.ensure", inputs={"label": "the label name to ensure"},
                             output="the label dict", primitives=["rest_get", "rest_post"],
                             test_args={"label": "praxis-synth-test"})

    def synth(step, refs=None):
        memory.put_skill(db, step.operation, contract, ENSURE_LABEL_CODE)
        return SimpleNamespace(ok=True, operation=step.operation, attempts=[])

    return synth


class LabelAwareClient:
    """Models a repo where labels actually matter: adding a missing label 422s, and creating
    a label makes a later add-label succeed. Lets the 422 -> labels.ensure -> retry path be
    exercised end-to-end offline against the real inverse-derivation machinery."""

    def __init__(self, existing_labels=("bug",), repo="o/r"):
        self.repo = repo
        self.api_calls = 0
        self.journal = None
        self.calls = []
        self.labels = set(existing_labels)
        self.label_422_count = 0
        self._issue_no = 0

    def _journaled(self, op, path, body, resp):
        if self.journal is not None:
            inv = inverse_of(op, path, body, resp)
            if inv is not None:
                self.journal.append(inv)
        return resp

    def rest_get(self, path, params=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_get", "path": path, "body": None})
        if path.endswith("/labels") and "/issues/" not in path:   # list repo labels
            return [{"id": 100, "name": n} for n in sorted(self.labels)]
        return []

    def rest_post(self, path, json=None):
        body = json or {}
        self.api_calls += 1
        self.calls.append({"op": "rest_post", "path": path, "body": body})
        if path.endswith("/issues"):
            self._issue_no += 1
            return self._journaled("rest_post", path, body, {"number": self._issue_no})
        if "/issues/" in path and path.endswith("/labels"):       # add labels to an issue
            for n in body.get("labels", []):
                if n not in self.labels:
                    self.label_422_count += 1
                    raise GitHubError(422, f"Label does not exist: {n}", "rest_post", path)
            return self._journaled("rest_post", path, body, [{"name": n} for n in body.get("labels", [])])
        if path.endswith("/labels"):                              # create a repo label
            name = body.get("name")
            self.labels.add(name)
            return self._journaled("rest_post", path, body, {"id": 200, "name": name})
        return self._journaled("rest_post", path, body, {})

    def rest_patch(self, path, json=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_patch", "path": path, "body": json or {}})
        return self._journaled("rest_patch", path, json or {}, {})

    def rest_delete(self, path, json=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_delete", "path": path, "body": json or {}})
        return self._journaled("rest_delete", path, json or {}, {})


# The synthesised milestones.ensure skill: resolve-or-create a milestone by title via
# primitives, returning the milestone dict (with its number). The live GitHub constraint the
# whole headline rests on — you cannot set a milestone by title, only by an existing number.
ENSURE_MILESTONE_CODE = (
    "def skill(client, milestone):\n"
    "    existing = client.rest_get('/repos/' + client.repo + '/milestones')\n"
    "    for ms in existing:\n"
    "        if ms['title'] == milestone:\n"
    "            return ms\n"
    "    return client.rest_post('/repos/' + client.repo + '/milestones', json={'title': milestone})\n"
)


def ensure_milestone_synth(db):
    contract = SkillContract(name="milestones.ensure",
                             inputs={"milestone": "the milestone title to resolve or create"},
                             output="the milestone dict (with its number)",
                             primitives=["rest_get", "rest_post"],
                             test_args={"milestone": "praxis-synth-test"})

    def synth(step, refs=None):
        memory.put_skill(db, step.operation, contract, ENSURE_MILESTONE_CODE)
        return SimpleNamespace(ok=True, operation=step.operation, attempts=[])

    return synth


class MilestoneAwareClient:
    """Models the REAL GitHub milestone constraint: PATCHing an issue's milestone with a title
    or a nonexistent number 422s; only an existing milestone NUMBER is accepted. Creating a
    milestone (POST) returns its assigned number."""

    def __init__(self, existing_titles=(), repo="o/r"):
        self.repo = repo
        self.api_calls = 0
        self.journal = None
        self.calls = []
        self.milestones = {}          # title -> number
        self.milestone_422_count = 0
        self._issue_no = 0
        self._next_ms = 0
        for t in existing_titles:
            self._next_ms += 1
            self.milestones[t] = self._next_ms

    def _journaled(self, op, path, body, resp):
        if self.journal is not None:
            inv = inverse_of(op, path, body, resp)
            if inv is not None:
                self.journal.append(inv)
        return resp

    def rest_get(self, path, params=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_get", "path": path, "body": None})
        if path.endswith("/milestones"):
            return [{"number": n, "title": t}
                    for t, n in sorted(self.milestones.items(), key=lambda kv: kv[1])]
        return []

    def rest_post(self, path, json=None):
        body = json or {}
        self.api_calls += 1
        self.calls.append({"op": "rest_post", "path": path, "body": body})
        if path.endswith("/issues"):
            self._issue_no += 1
            return self._journaled("rest_post", path, body, {"number": self._issue_no})
        if path.endswith("/milestones"):
            self._next_ms += 1
            self.milestones[body["title"]] = self._next_ms
            return self._journaled("rest_post", path, body,
                                   {"number": self._next_ms, "title": body["title"]})
        return self._journaled("rest_post", path, body, {})

    def rest_patch(self, path, json=None):
        body = json or {}
        self.api_calls += 1
        self.calls.append({"op": "rest_patch", "path": path, "body": body})
        ms = body.get("milestone", "__absent__")
        if ms not in ("__absent__", None) and not (isinstance(ms, int) and ms in self.milestones.values()):
            self.milestone_422_count += 1
            raise GitHubError(422, f"invalid milestone {ms}", "rest_patch", path)
        return self._journaled("rest_patch", path, body, {})

    def rest_delete(self, path, json=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_delete", "path": path, "body": json or {}})
        return self._journaled("rest_delete", path, json or {}, {})


def test_set_milestone_422_learns_rule_and_retries_via_ensure(db):
    # run 1: set_milestone by title 422s -> learn the precondition, synthesise+run
    # milestones.ensure (creates the milestone, caches its number), retry with the NUMBER.
    client = MilestoneAwareClient(existing_titles=())             # "Sprint 1" is missing
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "Login bug"}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api", args={"milestone": "Sprint 1"}),
    ]
    results = ex.run(run_id=1, steps=steps)

    rules = memory.rules_for(db, "issues.set_milestone")
    assert any(r["rule_type"] == "precondition" and r["detail"]["action"] == "milestones.ensure"
               and r["detail"]["param"] == "milestone" and r["learned_in_run"] == 1
               for r in rules), "the issues.set_milestone precondition rule must be learned in run 1"
    assert "Sprint 1" in client.milestones, "milestones.ensure must have created the missing milestone"
    assert memory.get_ref(db, "milestone:Sprint 1") is not None, "the resolved number must be cached"
    assert any(r.operation == "milestones.ensure" and r.status == "done" for r in results)
    sm = [r for r in results if r.operation == "issues.set_milestone"]
    assert any(r.status == "done" for r in sm), "set_milestone must succeed on the retry"
    # the successful PATCH used the resolved NUMBER, not the title
    patches = [c for c in client.calls if c["op"] == "rest_patch" and isinstance(c["body"].get("milestone"), int)]
    assert patches and patches[-1]["body"]["milestone"] == client.milestones["Sprint 1"]


def test_known_milestone_precondition_preapplies_and_resolves_number(db):
    # run 2: the rule is known and the number cached -> milestones.ensure is pre-applied
    # (ref-cache hit, no API), and set_milestone uses the cached NUMBER -> zero 422s.
    memory.add_rule(db, "issues.set_milestone", "precondition",
                    {"action": "milestones.ensure", "param": "milestone"}, learned_in_run=1)
    memory.put_ref(db, "milestone:Sprint 1", "milestone", "1", run_id=1)
    client = MilestoneAwareClient(existing_titles=("Sprint 1",))  # number 1 persists in the repo
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "X"}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api", args={"milestone": "Sprint 1"}),
    ]
    results = ex.run(run_id=2, steps=steps)

    assert client.milestone_422_count == 0, "pre-applied rule + cached number -> zero 422s"
    assert not any(r.status == "failed" for r in results)
    patches = [c for c in client.calls if c["op"] == "rest_patch" and "milestone" in c["body"]]
    assert patches and patches[0]["body"]["milestone"] == 1, "set_milestone must use the resolved number"
    resolve_calls = [c for c in client.calls if c["path"].endswith("/milestones")]
    assert resolve_calls == [], "a ref-cache hit must skip the milestone resolve entirely"


def test_stale_cached_milestone_number_self_heals_on_422(db):
    # the bug a real run surfaced: ref_cache holds a milestone NUMBER that was since deleted on
    # the platform (live "Sprint 1" is now a different number). The cached id must not loop on
    # 422 forever — on the 422 the executor evicts the stale ref, re-runs milestones.ensure
    # against the LIVE repo (cache miss -> resolve), and the retry succeeds with the right number.
    memory.add_rule(db, "issues.set_milestone", "precondition",
                    {"action": "milestones.ensure", "param": "milestone"}, learned_in_run=1)
    memory.put_ref(db, "milestone:Sprint 1", "milestone", "2", run_id=1)   # STALE: #2 was deleted
    client = MilestoneAwareClient(existing_titles=("Sprint 1",))           # live "Sprint 1" == #1
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "X"}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api",
             args={"milestone": "Sprint 1"}),
    ]
    results = ex.run(run_id=2, steps=steps)

    sm = [r for r in results if r.operation == "issues.set_milestone"]
    assert any(r.status == "done" for r in sm), "set_milestone must self-heal and succeed, not loop on 422"
    live_number = client.milestones["Sprint 1"]
    assert memory.get_ref(db, "milestone:Sprint 1") == str(live_number), \
        "the stale ref must be replaced with the live milestone number"
    patches = [c for c in client.calls
               if c["op"] == "rest_patch" and isinstance(c["body"].get("milestone"), int)]
    assert patches and patches[-1]["body"]["milestone"] == live_number, \
        "the successful PATCH must use the re-resolved live number, not the stale cached one"


def test_add_label_422_learns_precondition_rule_and_retries_via_ensure(db):
    # run 1: a bare add_label on a missing label 422s -> the executor extracts the
    # precondition rule, persists it (learned_in_run=1), injects + synthesises labels.ensure,
    # then retries the add_label once and succeeds.
    client = LabelAwareClient(existing_labels={"bug"})            # priority:high is missing
    ex = Executor(db, client, synthesizer=ensure_label_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "Login bug"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "priority:high"}),
    ]
    results = ex.run(run_id=1, steps=steps)

    rules = memory.rules_for(db, "issues.add_label")
    assert any(r["rule_type"] == "precondition" and r["detail"]["action"] == "labels.ensure"
               and r["detail"]["param"] == "label" and r["learned_in_run"] == 1
               for r in rules), "the issues.add_label precondition rule must be learned in run 1"
    assert "priority:high" in client.labels, "labels.ensure must have created the missing label"
    assert any(r.operation == "labels.ensure" and r.status == "done" for r in results), \
        "a labels.ensure step must have been injected and run"
    add_results = [r for r in results if r.operation == "issues.add_label"]
    assert any(r.status == "done" for r in add_results), "add_label must succeed on the retry"
    assert ex.synthesis_events and ex.synthesis_events[0]["operation"] == "labels.ensure"


def test_preapplied_precondition_is_recorded_for_the_report(db):
    # when a rule is already known, the executor records that it pre-applied it (with the run
    # it was learned in), so the report can state "pre-applied rule (learned run #1)".
    memory.add_rule(db, "issues.add_label", "precondition",
                    {"action": "labels.ensure", "param": "label"}, learned_in_run=1)
    client = LabelAwareClient(existing_labels={"bug", "priority:high"})
    ex = Executor(db, client, synthesizer=ensure_label_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "X"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "priority:high"}),
    ]
    ex.run(run_id=2, steps=steps)
    assert ex.preapplied_rules == [
        {"operation": "issues.add_label", "action": "labels.ensure",
         "param": "label", "learned_in_run": 1}
    ]


def test_run_one_does_not_record_a_preapplied_rule_when_learning_fresh(db):
    # run 1 LEARNS the rule mid-run (it isn't pre-applied), so preapplied_rules stays empty.
    client = LabelAwareClient(existing_labels={"bug"})
    ex = Executor(db, client, synthesizer=ensure_label_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "X"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "priority:high"}),
    ]
    ex.run(run_id=1, steps=steps)
    assert ex.preapplied_rules == []


def test_labels_ensure_caches_id_then_serves_from_ref_cache(db):
    # run 1: labels.ensure resolves/creates priority:high and caches its id (resolve-once).
    memory.add_rule(db, "issues.add_label", "precondition",
                    {"action": "labels.ensure", "param": "label"}, learned_in_run=1)
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "X"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "priority:high"}),
    ]
    c1 = LabelAwareClient(existing_labels={"bug"})
    Executor(db, c1, synthesizer=ensure_label_synth(db)).run(run_id=1, steps=steps)
    assert memory.get_ref(db, "label:priority:high") is not None, "the resolved label id must be cached"

    # run 2 (same DB, same repo state — the label now persists in the repo): the ref-cache hit
    # serves the id without re-running labels.ensure, so it makes ZERO label-resolve API calls.
    c2 = LabelAwareClient(existing_labels={"bug", "priority:high"})
    Executor(db, c2, synthesizer=ensure_label_synth(db)).run(run_id=2, steps=steps)
    resolve_calls = [c for c in c2.calls if c["path"].endswith("/labels") and "/issues/" not in c["path"]]
    assert resolve_calls == [], "a ref-cache hit must skip the label resolve entirely"


def test_known_precondition_preapplies_ensure_before_add_label(db):
    # run 2 (a second executor): the rule is already in learned_rules, so labels.ensure is
    # injected BEFORE the bare add_label and the run takes zero 422s.
    memory.add_rule(db, "issues.add_label", "precondition",
                    {"action": "labels.ensure", "param": "label"}, learned_in_run=1)
    client = LabelAwareClient(existing_labels={"bug"})            # priority:high still missing
    ex = Executor(db, client, synthesizer=ensure_label_synth(db))
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "X"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "priority:high"}),
    ]
    results = ex.run(run_id=2, steps=steps)

    assert client.label_422_count == 0, "a pre-applied precondition must avoid the 422 entirely"
    assert not any(r.status == "failed" for r in results), "no failures when the rule is pre-applied"
    ops = [r.operation for r in results]
    assert "labels.ensure" in ops and ops.index("labels.ensure") < ops.index("issues.add_label"), \
        "labels.ensure must be injected before issues.add_label"


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


def test_add_label_targets_created_issue_without_explicit_arg(db):
    # the planner can't know the runtime issue number; the executor threads it from the
    # preceding create so enrichment steps reach the right issue.
    client = FakeClient()
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api", args={"label": "bug"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[0].status == "done"
    assert results[1].status == "done"
    label_calls = [c for c in client.calls if c["op"] == "rest_post" and c["path"].endswith("/labels")]
    assert label_calls and label_calls[0]["path"] == "/repos/o/r/issues/1/labels"


def test_add_label_ignores_placeholder_issue_ref(db):
    # planners sometimes emit a placeholder like {"issue": "ctx:from_step1"}; that must
    # not become the URL path — resolve the real created issue from run context instead.
    client = FakeClient()
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api",
             args={"issue": "ctx:from_step1", "label": "bug"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[1].status == "done"
    label_calls = [c for c in client.calls if c["path"].endswith("/labels")]
    assert label_calls[0]["path"] == "/repos/o/r/issues/1/labels"


def test_add_label_honors_explicit_numeric_issue(db):
    client = FakeClient()
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api",
             args={"issue_number": "1", "label": "bug"}),  # digit string + alt key
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[1].status == "done"
    label_calls = [c for c in client.calls if c["path"].endswith("/labels")]
    assert label_calls[0]["path"] == "/repos/o/r/issues/1/labels"


def test_set_milestone_targets_created_issue_without_explicit_arg(db):
    client = FakeClient()
    steps = [
        Step(seq=1, intent="create", operation="issues.create", kind="api", args={"title": "A"}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api", args={"milestone": 3}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert results[1].status == "done"
    patch_calls = [c for c in client.calls if c["op"] == "rest_patch"]
    assert patch_calls and patch_calls[0]["path"] == "/repos/o/r/issues/1"


def _compute_contract(op):
    return SkillContract(name=op, inputs={"issues": "list of issue dicts"},
                         output="markdown table", primitives=[], test_args={"issues": []})


def test_compute_op_synthesises_then_runs(db):
    # a compute.* step with no registered skill -> synthesize once, then run the skill.
    # the skill's `issues` kwarg is threaded from the preceding issues.list output.
    client = FakeClient()
    code = "def skill(client, issues):\n    return 'TABLE rows=' + str(len(issues))"

    def fake_synth(step, refs=None):
        memory.put_skill(db, step.operation, _compute_contract(step.operation), code)
        return SimpleNamespace(ok=True, operation=step.operation, attempts=[])

    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={}),
        Step(seq=2, intent="group", operation="compute.group_by_label_and_render_table",
             kind="compute", args={}),
    ]
    ex = Executor(db, client, synthesizer=fake_synth)
    results = ex.run(run_id=1, steps=steps)
    assert results[1].status == "done"
    assert ex.synthesis_events and ex.synthesis_events[0]["operation"].startswith("compute.")
    assert memory.get_skill(db, "compute.group_by_label_and_render_table") is not None


def test_compute_op_reuses_registered_skill_without_synthesising(db):
    client = FakeClient()
    memory.put_skill(db, "compute.group_by_label_and_render_table",
                     _compute_contract("compute.group_by_label_and_render_table"),
                     "def skill(client, issues):\n    return len(issues)")

    def boom(step, refs=None):
        raise AssertionError("must not synthesize when a skill is already registered")

    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={}),
        Step(seq=2, intent="group", operation="compute.group_by_label_and_render_table",
             kind="compute", args={}),
    ]
    ex = Executor(db, client, synthesizer=boom)
    results = ex.run(run_id=1, steps=steps)
    assert results[1].status == "done"
    assert ex.synthesis_events == []


def test_create_body_includes_latest_compute_result(db):
    # the planner can't embed the runtime grouped table; the executor threads the latest
    # compute output into the triage issue's body so the issue actually lists them.
    client = FakeClient()
    memory.put_skill(db, "compute.group_by_label_and_render_table",
                     _compute_contract("compute.group_by_label_and_render_table"),
                     "def skill(client, issues):\n    return '| label |\\n| bug |'")
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={}),
        Step(seq=2, intent="group", operation="compute.group_by_label_and_render_table",
             kind="compute", args={}),
        Step(seq=3, intent="summary", operation="issues.create", kind="api",
             args={"title": "Triage summary", "body": "Open unassigned issues by label:"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert all(r.status == "done" for r in results)
    create = [c for c in client.calls if c["op"] == "rest_post" and c["path"].endswith("/issues")]
    assert create and "| label |" in create[0]["body"]["body"], "triage body must carry the table"
    assert "Open unassigned issues by label:" in create[0]["body"]["body"], "planner text kept"


def test_create_body_placeholder_is_replaced_by_compute_result(db):
    # the planner often emits a lone data-flow placeholder for the body ("$steps.2.result"
    # / "{{ table }}"); the executor must REPLACE it with the table, not prepend the junk.
    client = FakeClient()
    memory.put_skill(db, "compute.group_by_label_and_render_table",
                     _compute_contract("compute.group_by_label_and_render_table"),
                     "def skill(client, issues):\n    return '| label |\\n| bug |'")
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={}),
        Step(seq=2, intent="group", operation="compute.group_by_label_and_render_table",
             kind="compute", args={}),
        Step(seq=3, intent="summary", operation="issues.create", kind="api",
             args={"title": "Triage", "body": "$steps.2.result"}),
    ]
    Executor(db, client).run(run_id=1, steps=steps)
    create = [c for c in client.calls if c["op"] == "rest_post" and c["path"].endswith("/issues")]
    body = create[0]["body"]["body"]
    assert "| label |" in body and "$steps.2.result" not in body, "placeholder must be replaced"


def test_compute_op_failed_synthesis_is_fatal(db):
    client = FakeClient()

    def failing_synth(step, refs=None):
        return SimpleNamespace(ok=False, operation=step.operation,
                               attempts=["e1", "e2", "e3"])

    steps = [
        Step(seq=1, intent="group", operation="compute.broken", kind="compute", args={}),
    ]
    ex = Executor(db, client, synthesizer=failing_synth)
    results = ex.run(run_id=1, steps=steps)
    assert results[0].status == "failed"          # compute failure is not enrichment -> fatal
    assert ex.synthesis_events and ex.synthesis_events[0]["ok"] is False


class TriageClient:
    """A repo with a fixed set of issues for the Instruction-3 triage fan-out. Adding a label
    auto-creates it (never 422, per the live GitHub finding); setting a milestone by a title or a
    nonexistent number 422s — only an existing milestone NUMBER is accepted. issues.list returns
    the snapshot the fan-out filters over."""

    def __init__(self, issues=None, existing_milestones=(), repo="o/r"):
        self.repo = repo
        self.api_calls = 0
        self.journal = None
        self.calls = []
        self.issues = issues if issues is not None else self._default_issues()
        self.milestones = {}             # title -> number
        self._next_ms = 0
        self.milestone_422_count = 0
        for t in existing_milestones:
            self._next_ms += 1
            self.milestones[t] = self._next_ms

    @staticmethod
    def _default_issues():
        # #1,#2 are open/unassigned/untriaged (the fan-out targets); #3 is assigned; #4 already
        # carries needs-triage. Snapshot shape mirrors the real GitHub issue JSON.
        return [
            {"number": 1, "assignee": None, "labels": ["bug"], "milestone": None, "state": "open"},
            {"number": 2, "assignee": None, "labels": ["documentation"], "milestone": None, "state": "open"},
            {"number": 3, "assignee": {"login": "dev"}, "labels": ["enhancement"], "milestone": None, "state": "open"},
            {"number": 4, "assignee": None, "labels": ["needs-triage"], "milestone": None, "state": "open"},
        ]

    def _journaled(self, op, path, body, resp):
        if self.journal is not None:
            inv = inverse_of(op, path, body, resp)
            if inv is not None:
                self.journal.append(inv)
        return resp

    def rest_get(self, path, params=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_get", "path": path, "body": None})
        if path.endswith("/issues") and "/issues/" not in path:
            return [{"number": i["number"], "assignee": i["assignee"],
                     "labels": [{"name": n} for n in i["labels"]],
                     "milestone": ({"number": i["milestone"]} if i["milestone"] else None),
                     "state": i["state"]}
                    for i in self.issues if i["state"] == "open"]
        if path.endswith("/milestones"):
            return [{"number": n, "title": t}
                    for t, n in sorted(self.milestones.items(), key=lambda kv: kv[1])]
        return []

    def rest_post(self, path, json=None):
        body = json or {}
        self.api_calls += 1
        self.calls.append({"op": "rest_post", "path": path, "body": body})
        if "/issues/" in path and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            for i in self.issues:
                if i["number"] == num:
                    for n in body.get("labels", []):
                        if n not in i["labels"]:
                            i["labels"].append(n)
            return self._journaled("rest_post", path, body, [{"name": n} for n in body.get("labels", [])])
        if path.endswith("/milestones"):
            self._next_ms += 1
            self.milestones[body["title"]] = self._next_ms
            return self._journaled("rest_post", path, body, {"number": self._next_ms, "title": body["title"]})
        if path.endswith("/issues"):
            return self._journaled("rest_post", path, body, {"number": 99})
        return self._journaled("rest_post", path, body, {})

    def rest_patch(self, path, json=None):
        body = json or {}
        self.api_calls += 1
        self.calls.append({"op": "rest_patch", "path": path, "body": body})
        if "milestone" in body:
            ms = body["milestone"]
            if not (isinstance(ms, int) and ms in self.milestones.values()):
                self.milestone_422_count += 1
                raise GitHubError(422, f"invalid milestone {ms}", "rest_patch", path)
            num = int(path.split("/issues/")[1].split("/")[0])
            for i in self.issues:
                if i["number"] == num:
                    i["milestone"] = ms
        return self._journaled("rest_patch", path, body, {})

    def rest_delete(self, path, json=None):
        self.api_calls += 1
        self.calls.append({"op": "rest_delete", "path": path, "body": json or {}})
        return self._journaled("rest_delete", path, json or {}, {})


def _labeled_issue_numbers(client):
    return sorted(int(c["path"].split("/issues/")[1].split("/")[0])
                  for c in client.calls
                  if c["op"] == "rest_post" and c["path"].endswith("/labels"))


def test_add_label_fans_out_over_open_unassigned_untriaged(db):
    # "target": "all" expands one add_label step over the issues.list snapshot, applying it to
    # every open + unassigned issue that doesn't already carry the label (idempotency filter).
    client = TriageClient()
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api",
             args={"filters": {"state": "open"}}),
        Step(seq=2, intent="triage label", operation="issues.add_label", kind="api",
             args={"label": "needs-triage", "target": "all"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert all(r.status == "done" for r in results)
    # #3 is assigned, #4 already triaged -> only #1 and #2 get the label
    assert _labeled_issue_numbers(client) == [1, 2]


def test_add_label_fan_out_is_idempotent_on_rerun(db):
    # a second run over the same snapshot (now all triaged) applies the label to nobody.
    issues = [{"number": 1, "assignee": None, "labels": ["bug", "needs-triage"],
               "milestone": None, "state": "open"}]
    client = TriageClient(issues=issues)
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={"filters": {"state": "open"}}),
        Step(seq=2, intent="label", operation="issues.add_label", kind="api",
             args={"label": "needs-triage", "target": "all"}),
    ]
    Executor(db, client).run(run_id=1, steps=steps)
    assert _labeled_issue_numbers(client) == [], "already-triaged issues are skipped"


class LabelFilterAwareTriageClient(TriageClient):
    """Like TriageClient, but its issues.list HONORS the GitHub `labels` query param as an
    INCLUSION filter (returns only issues that HAVE every listed label) — GitHub's real
    semantics, which has NO negation. Proves the executor must not forward a label-EXCLUSION
    intent (a planner's 'not-yet-triaged') to the list API, where it would invert the meaning
    and zero out the fan-out snapshot."""

    def rest_get(self, path, params=None):
        if path.endswith("/issues") and "/issues/" not in path:
            self.api_calls += 1
            self.calls.append({"op": "rest_get", "path": path, "body": None})
            want = (params or {}).get("labels")
            want_list = want if isinstance(want, list) else ([want] if want else [])
            return [{"number": i["number"], "assignee": i["assignee"],
                     "labels": [{"name": n} for n in i["labels"]],
                     "milestone": ({"number": i["milestone"]} if i["milestone"] else None),
                     "state": i["state"]}
                    for i in self.issues
                    if i["state"] == "open" and all(w in i["labels"] for w in want_list)]
        return super().rest_get(path, params)


def test_list_label_exclusion_is_not_forwarded_to_the_list_api(db):
    # the Instruction-3 bug a live run surfaced: the planner expresses "not-yet-triaged" as a
    # label EXCLUSION on the list step (labels=[needs-triage] + label_mode="none"). GitHub's list
    # API has no negation, so forwarding it verbatim returns the issues that HAVE the label (here
    # only #4) and the fan-out enriches nobody — a silent no-op. The exclusion must be applied
    # client-side (skip_if_label) against the pre-enrichment snapshot, not sent to the API.
    client = LabelFilterAwareTriageClient()
    steps = [
        Step(seq=1, intent="list open unassigned not-yet-triaged", operation="issues.list",
             kind="api", args={"filters": {"state": "open", "assignee": "none",
                                           "labels": ["needs-triage"], "label_mode": "none"}}),
        Step(seq=2, intent="triage label", operation="issues.add_label", kind="api",
             args={"label": "needs-triage", "target": "all", "skip_if_label": "needs-triage"}),
    ]
    results = Executor(db, client).run(run_id=1, steps=steps)
    assert all(r.status == "done" for r in results)
    # #1,#2 are open/unassigned/untriaged -> they must get the label (#3 assigned, #4 already triaged)
    assert _labeled_issue_numbers(client) == [1, 2], \
        "a label-exclusion on the list step must not zero out the fan-out snapshot"


def test_set_milestone_fan_out_cold_learns_then_applies_to_all(db):
    # cold: set_milestone by title fans out, the first patch 422s -> learn the precondition,
    # synthesise+run milestones.ensure (creates + caches Sprint 1), retry -> every target issue
    # ends up on the resolved milestone NUMBER, zero final failures.
    client = TriageClient()                       # Sprint 1 does not exist yet
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db))
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={"filters": {"state": "open"}}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api",
             args={"milestone": "Sprint 1", "target": "all", "skip_if_label": "needs-triage"}),
    ]
    results = ex.run(run_id=1, steps=steps)

    rules = memory.rules_for(db, "issues.set_milestone")
    assert any(r["rule_type"] == "precondition" and r["learned_in_run"] == 1 for r in rules), \
        "the set_milestone precondition rule must be learned in run 1"
    assert "Sprint 1" in client.milestones, "milestones.ensure created the missing milestone"
    assert memory.get_ref(db, "milestone:Sprint 1") is not None, "the number must be cached"
    num = client.milestones["Sprint 1"]
    on_ms = sorted(i["number"] for i in client.issues if i["milestone"] == num)
    assert on_ms == [1, 2], "every open/unassigned issue lands on the resolved milestone number"
    sm = [r for r in results if r.operation == "issues.set_milestone"]
    assert any(r.status == "done" for r in sm), "set_milestone succeeds on the retry"


def test_set_milestone_fan_out_warm_preapplies_and_takes_zero_422(db):
    # warm (the cross-instruction transfer): the rule is known + Sprint 1 cached -> milestones.ensure
    # is pre-applied (ref-cache hit, no resolve API), and the fan-out patches every target with the
    # cached NUMBER -> zero 422s.
    memory.add_rule(db, "issues.set_milestone", "precondition",
                    {"action": "milestones.ensure", "param": "milestone"}, learned_in_run=1)
    memory.put_ref(db, "milestone:Sprint 1", "milestone", "1", run_id=1)
    client = TriageClient(existing_milestones=("Sprint 1",))   # number 1 already in the repo
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db))
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={"filters": {"state": "open"}}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api",
             args={"milestone": "Sprint 1", "target": "all", "skip_if_label": "needs-triage"}),
    ]
    results = ex.run(run_id=1, steps=steps)

    assert client.milestone_422_count == 0, "pre-applied rule + cached number -> zero 422s"
    assert not any(r.status == "failed" for r in results)
    assert ex.preapplied_rules and ex.preapplied_rules[0]["operation"] == "issues.set_milestone"
    on_ms = sorted(i["number"] for i in client.issues if i["milestone"] == 1)
    assert on_ms == [1, 2], "every target issue set to the cached milestone number"
    resolve_calls = [c for c in client.calls if c["path"].endswith("/milestones")]
    assert resolve_calls == [], "a ref-cache hit must skip the milestone resolve entirely"


def test_no_learning_disables_recovery_and_learning(db):
    # the cold baseline: with learning off, a set_milestone-by-title 422 is NOT recovered and NO
    # rule is learned -> the enrichment fails (non-fatal), the milestone is never created.
    client = TriageClient()                       # Sprint 1 does not exist, nothing cached
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db), learning_enabled=False)
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={"filters": {"state": "open"}}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api",
             args={"milestone": "Sprint 1", "target": "all", "skip_if_label": "needs-triage"}),
    ]
    results = ex.run(run_id=1, steps=steps)
    assert memory.rules_for(db, "issues.set_milestone") == [], "no rule learned with learning off"
    sm = [r for r in results if r.operation == "issues.set_milestone"]
    assert sm and all(r.status == "failed" for r in sm), "the 422 is not recovered"
    assert "Sprint 1" not in client.milestones, "milestones.ensure never ran"


def test_no_learning_skips_preapply_even_with_known_rule(db):
    # even with the precondition rule already in memory, learning off must NOT inject the
    # prerequisite milestones.ensure -> the bare set_milestone 422s and is not recovered.
    memory.add_rule(db, "issues.set_milestone", "precondition",
                    {"action": "milestones.ensure", "param": "milestone"}, learned_in_run=1)
    client = TriageClient()                       # number NOT cached, Sprint 1 NOT in repo
    ex = Executor(db, client, synthesizer=ensure_milestone_synth(db), learning_enabled=False)
    steps = [
        Step(seq=1, intent="list", operation="issues.list", kind="api", args={"filters": {"state": "open"}}),
        Step(seq=2, intent="milestone", operation="issues.set_milestone", kind="api",
             args={"milestone": "Sprint 1", "target": "all", "skip_if_label": "needs-triage"}),
    ]
    results = ex.run(run_id=1, steps=steps)
    ops = [r.operation for r in results]
    assert "milestones.ensure" not in ops, "no pre-applied ensure when learning is off"
    assert ex.preapplied_rules == []
    assert "Sprint 1" not in client.milestones


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
