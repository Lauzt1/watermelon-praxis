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
