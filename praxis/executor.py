"""Executor — runs typed steps against the platform, with the spec §9 failure policy.

Two dispatch classes (spec §5, "Dispatch class — Option A"):
  * single-endpoint ops bind directly to ONE primitive via a deterministic table;
  * compound / compute.* ops resolve through the skills registry, synthesising on first
    use (the Synthesizer arrives in Phase 2; until then a missing skill is an error).

Every successful mutation's inverse (produced by the adapter into `client.journal`) is
persisted to `undo_journal`, so a fatal failure can be auto-rolled-back by replaying it
in reverse. Failure policy: an *enrichment* op failing is non-fatal (keep the primary,
continue, run is `partial`); any other failure is fatal (stop, roll back, run is `failed`).
"""
from __future__ import annotations

import time
from typing import Any

from . import memory, operations, sandbox, synthesizer
from .models import Step, StepResult
from .platform.github import GitHubError

# A synthesised skill is in-process and pure-ish; if it overruns this it's a runaway, not
# slow I/O (effectful skills make a handful of fast calls). The watchdog turns that into a
# clean failure instead of a hang.
SKILL_TIMEOUT_S = 20.0

# Skill-health governance (spec §7.1): a skill used at least MIN_SAMPLES times that succeeds
# less than THRESHOLD of the time is quarantined, then re-synthesised on its next use.
SKILL_MIN_SAMPLES = 3
SKILL_CONFIDENCE_THRESHOLD = 0.5


class ExecutorError(Exception):
    """Raised for an operation the executor cannot dispatch (unknown, or a compound op
    with no registered skill and no synthesizer yet)."""


class Executor:
    def __init__(self, db, client, synthesizer=None, learning_enabled=True):
        self.db = db
        self.client = client
        self.synthesizer = synthesizer
        # When False (the `run --no-learning` cold baseline): no learned-rule pre-loading and
        # no 422 learn-and-retry recovery — the agent runs with zero memory so the on-camera
        # cold-vs-warm comparison is honest.
        self.learning_enabled = learning_enabled
        # Within-run references (e.g. the issue number a create produced, or the issue list
        # a compute step consumes). The planner can't know runtime values, so later steps
        # resolve them from here. Transient run state, distinct from the persistent
        # ref_cache (label/milestone ids) wired in Phase 3.
        self.run_refs: dict[str, Any] = {}
        # Synthesis events recorded this run, surfaced by the reporter.
        self.synthesis_events: list[dict[str, Any]] = []
        # Learned precondition rules pre-applied this run (the cross-run transfer evidence).
        self.preapplied_rules: list[dict[str, Any]] = []
        # Skill quarantine/heal events recorded this run, surfaced by the reporter (spec §7.1).
        self.skill_health_events: list[dict[str, Any]] = []
        # Marker labels a fan-out EXCLUDES (its skip_if_label / the label it adds). The list
        # sanitizer strips these from any inclusion filter so a planner's bare labels=[marker]
        # (no label_mode) can't invert "not-yet-triaged" into "only-already-triaged". Recomputed
        # per run() from the whole plan, since one step can't see the others' args.
        self._fan_out_skip_labels: frozenset[str] = frozenset()

    # --- dispatch ---------------------------------------------------------------

    def _issue_target(self, args: dict) -> Any:
        # Accept only an integer-like explicit issue; anything else (a missing key, or a
        # planner placeholder such as "ctx:issue_number_from_step1") resolves from the
        # within-run context populated by the preceding create.
        raw = args.get("issue", args.get("issue_number"))
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        issue = self.run_refs.get("last_issue")
        if issue is None:
            raise ExecutorError("no target issue (none usable in args and none created this run)")
        return issue

    def _resolve_milestone(self, value):
        """GitHub accepts only a milestone NUMBER, never a title. Resolve a title to its cached
        number (populated by milestones.ensure); an already-numeric value passes through; an
        unresolved title passes through unchanged so it 422s and triggers the precondition
        learning (which then runs milestones.ensure, caches the number, and the retry resolves)."""
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        cached = memory.get_ref(self.db, f"milestone:{value}")
        if cached is not None and str(cached).isdigit():
            return int(cached)
        return value

    # --- bulk fan-out (Instruction 3: enrich EVERY matching issue) ---------------

    # The planner signals "apply this enrichment to every listed issue" with one of these in
    # the step's args["target"] (canonical: "all"). Synonyms tolerate planner phrasing drift.
    _FAN_OUT_FLAGS = frozenset({"all", "all_listed", "all_open_unassigned", "all_matching", "every"})

    def _wants_fan_out(self, args: dict) -> bool:
        target = args.get("target")
        if isinstance(target, str) and target.strip().lower() in self._FAN_OUT_FLAGS:
            return True
        return bool(args.get("all") or args.get("fan_out"))

    def _fan_out_issues(self) -> list:
        """The issues.list snapshot a fan-out enriches. Read from run_refs (NOT re-fetched), so
        the 'not-yet-triaged' filter is evaluated against the pre-enrichment state and the two
        enrichment steps see a consistent target set regardless of order."""
        issues = self.run_refs.get("issues")
        return issues if isinstance(issues, list) else []

    @staticmethod
    def _open_unassigned(issue: dict) -> bool:
        return (issue.get("state", "open") == "open"
                and issue.get("assignee") in (None, {}, ""))

    @staticmethod
    def _label_names(issue: dict) -> set:
        return {l.get("name") for l in (issue.get("labels") or []) if isinstance(l, dict)}

    def _fan_out_targets(self, args: dict, default_skip_label: str | None = None) -> list:
        """Open + unassigned snapshot issues, minus any already carrying the 'already-triaged'
        marker label. The planner expresses 'not-yet-triaged' as args["skip_if_label"]
        ("needs-triage"); add_label defaults it to the label it adds (natural idempotency), and
        set_milestone inherits it from a preceding add_label fan-out — so both enrichment steps
        select the identical set regardless of order."""
        skip_label = args.get("skip_if_label", default_skip_label)
        targets = []
        for issue in self._fan_out_issues():
            if issue.get("number") is None or not self._open_unassigned(issue):
                continue
            if skip_label is not None and skip_label in self._label_names(issue):
                continue
            targets.append(issue)
        return targets

    def _fan_out_add_label(self, args: dict) -> dict:
        """Add the label to every not-yet-triaged open + unassigned snapshot issue. Labels
        auto-create, so no 422. Records the label as the run's triage marker so a later
        set_milestone fan-out skips the same already-triaged issues."""
        repo = self.client.repo
        label = args.get("label") or (args.get("labels") or [None])[0]
        self.run_refs["triage_label"] = label
        applied = []
        for issue in self._fan_out_targets(args, default_skip_label=label):
            num = issue["number"]
            self.client.rest_post(f"/repos/{repo}/issues/{num}/labels", json={"labels": [label]})
            applied.append(num)
        return {"fan_out": "issues.add_label", "label": label, "applied": applied}

    def _fan_out_set_milestone(self, args: dict) -> dict:
        """Set the milestone on every not-yet-triaged open + unassigned snapshot issue not already
        on it. On the cold path the title is unresolved -> the first PATCH 422s and raises,
        triggering the learn-and-retry; on retry (and on the warm path) the resolved NUMBER is
        applied to all."""
        repo = self.client.repo
        resolved = self._resolve_milestone(args.get("milestone"))
        applied, skipped = [], []
        for issue in self._fan_out_targets(args, default_skip_label=self.run_refs.get("triage_label")):
            num = issue["number"]
            current = issue.get("milestone")
            current_num = current.get("number") if isinstance(current, dict) else current
            if isinstance(resolved, int) and current_num == resolved:
                skipped.append(num)
                continue
            self.client.rest_patch(f"/repos/{repo}/issues/{num}", json={"milestone": resolved})
            applied.append(num)
        return {"fan_out": "issues.set_milestone", "milestone": resolved,
                "applied": applied, "skipped": skipped}

    # The query params GitHub's "list repository issues" endpoint actually understands. Anything
    # else a planner invents (e.g. "label_mode") must be dropped, never forwarded to the API.
    _GH_LIST_PARAMS = frozenset({
        "milestone", "state", "assignee", "creator", "mentioned", "labels",
        "since", "sort", "direction", "per_page", "page", "filter",
    })
    # Planner phrasings that mean "exclude these labels" (i.e. not-yet-triaged). GitHub's list
    # endpoint has NO label negation, so under any of these we must NOT send `labels` as an
    # inclusion filter — the exclusion is applied client-side by the fan-out's skip_if_label.
    _LABEL_EXCLUSION_MODES = frozenset({"none", "not", "exclude", "without", "negate", "absent"})

    @classmethod
    def _sanitize_list_filters(cls, filters, exclude_labels=frozenset()):
        """Keep only GitHub-recognized list params, and strip a label-EXCLUSION so it never reaches
        the API as an inclusion. The planner expresses 'not-yet-triaged' as labels=[X]+label_mode=
        "none" (or a '!'-prefixed label); forwarded verbatim that returns the issues that HAVE X —
        the exact opposite — emptying the fan-out snapshot into a silent no-op. Exclusion is the
        fan-out's job (skip_if_label) against the pre-enrichment snapshot, not the list query's.

        `exclude_labels` are the plan's fan-out marker labels: even a BARE labels=[marker] with no
        label_mode (what a live run's planner actually emitted) is an exclusion if a downstream
        fan-out skips/adds that same label — strip just those, keeping genuine inclusions."""
        if not isinstance(filters, dict):
            return filters
        mode = str(filters.get("label_mode", "")).strip().lower()
        raw = filters.get("labels", filters.get("label"))
        label_list = raw if isinstance(raw, list) else ([raw] if raw else [])
        excluding = (mode in cls._LABEL_EXCLUSION_MODES
                     or any(isinstance(l, str) and l.strip().startswith("!") for l in label_list))
        clean = {k: v for k, v in filters.items() if k in cls._GH_LIST_PARAMS}
        if excluding:
            clean.pop("labels", None)
        elif exclude_labels and "labels" in clean:
            kept = [l for l in label_list
                    if not (isinstance(l, str) and l.strip() in exclude_labels)]
            if not kept:
                clean.pop("labels", None)
            elif len(kept) != len(label_list):
                clean["labels"] = kept
        return clean

    def _collect_fan_out_skip_labels(self, steps) -> frozenset[str]:
        """The marker labels any fan-out step in this plan EXCLUDES: its skip_if_label, plus the
        label a fan-out add_label adds (which defaults its own skip — natural idempotency). These
        are the labels that must never be forwarded as a list inclusion."""
        markers: set[str] = set()
        for step in steps:
            args = getattr(step, "args", None) or {}
            if not self._wants_fan_out(args):
                continue
            skip = args.get("skip_if_label")
            if isinstance(skip, str) and skip.strip():
                markers.add(skip.strip())
            if step.operation == "issues.add_label":
                lab = args.get("label") or (args.get("labels") or [None])[0]
                if isinstance(lab, str) and lab.strip():
                    markers.add(lab.strip())
        return frozenset(markers)

    def _dispatch(self, step: Step) -> Any:
        op, args, repo = step.operation, step.args, self.client.repo

        if op == "issues.create":
            body = {k: args[k] for k in ("title", "body", "assignees", "labels", "milestone") if k in args}
            body = self._thread_compute_result(body)
            resp = self.client.rest_post(f"/repos/{repo}/issues", json=body)
            if isinstance(resp, dict) and resp.get("number") is not None:
                self.run_refs["last_issue"] = resp["number"]
            return resp
        if op == "issues.add_label":
            if self._wants_fan_out(args):
                return self._fan_out_add_label(args)
            issue = self._issue_target(args)
            labels = args.get("labels") or [args["label"]]
            return self.client.rest_post(f"/repos/{repo}/issues/{issue}/labels", json={"labels": labels})
        if op == "issues.set_milestone":
            if self._wants_fan_out(args):
                return self._fan_out_set_milestone(args)
            issue = self._issue_target(args)
            milestone = self._resolve_milestone(args.get("milestone"))
            return self.client.rest_patch(f"/repos/{repo}/issues/{issue}", json={"milestone": milestone})
        if op == "issues.list":
            filters = self._sanitize_list_filters(args.get("filters") or args,
                                                  self._fan_out_skip_labels)
            resp = self.client.rest_get(f"/repos/{repo}/issues", params=filters or None)
            self.run_refs["issues"] = resp        # thread the list to a downstream compute step
            return resp

        if op in ("labels.ensure", "milestones.ensure"):
            # Compound + effectful: synthesised on first use, then run sandboxed. Seed the
            # subject (label name / milestone title) into run_refs so the shared kwargs
            # resolver maps the REAL subject onto whatever input name the contract chose,
            # instead of falling back to the synthesis test placeholder.
            kind = "label" if op == "labels.ensure" else "milestone"
            subject = step.args.get(kind) or (step.args.get("labels") or [None])[0]
            if subject is not None:
                self.run_refs[kind] = subject     # seed for resolve_skill_kwargs aliasing
                cached = memory.get_ref(self.db, f"{kind}:{subject}")
                if cached is not None:
                    # resolve-once-cache-forever: a prior run already resolved this id, so
                    # skip the skill and its API calls entirely (the declining call count).
                    self.run_refs[op] = cached
                    return cached
            result = self._dispatch_skill(step)
            self.run_refs[op] = result
            if subject is not None:
                value = self._ensure_ref_value(result, kind, subject)
                memory.put_ref(self.db, f"{kind}:{subject}", kind, str(value), run_id=self.run_id)
            return result

        if operations.is_compound(op):
            result = self._dispatch_skill(step)
            self.run_refs[op] = result            # e.g. run_refs["compute.group_by_label..."]
            if operations.is_compute(op):
                self.run_refs["result"] = result  # friendly alias for the latest transform output
            return result

        raise ExecutorError(f"unknown operation {op!r}")

    @staticmethod
    def _looks_like_placeholder(text: str) -> bool:
        """A lone data-flow placeholder the planner emitted for the body, e.g.
        "$steps.2.result", "{{ table }}", "ctx:result", "<table>" — to be replaced, not kept."""
        t = (text or "").strip()
        if not t:
            return True
        if t.startswith("$") or t.startswith("ctx:"):
            return True
        return any(t.startswith(a) and t.endswith(b)
                   for a, b in (("{{", "}}"), ("{", "}"), ("<", ">"), ("[", "]")))

    def _thread_compute_result(self, body: dict) -> dict:
        """If a compute transform produced text this run, fold it into the issue body. The
        planner can't embed a runtime-computed table, so the executor injects it: a lone
        placeholder body is replaced; real prose is kept and the table appended."""
        result = self.run_refs.get("result")
        if not isinstance(result, str) or not result.strip():
            return body
        existing = body.get("body") or ""
        if result in existing:
            return body
        body = dict(body)
        if self._looks_like_placeholder(existing):
            body["body"] = result
        else:
            body["body"] = (existing.rstrip() + "\n\n" + result).strip()
        return body

    def _dispatch_skill(self, step: Step) -> Any:
        """Look up the skill; self-heal it if quarantined; synthesise it on first use; then
        compile + run it sandboxed, recording the outcome into its health stats (spec §7.1)."""
        skill = memory.get_skill(self.db, step.operation)
        if skill is not None and skill["status"] == "quarantined":
            skill = self._heal_quarantined(step, skill)   # rebuild before use, or raise
        if skill is None:
            if self.synthesizer is None:
                raise ExecutorError(
                    f"no registered skill for {step.operation!r} and no synthesizer"
                )
            # pass the run's references so a pure transform is tested on real upstream data
            result = self.synthesizer(step, self.run_refs)  # reason -> build -> test -> register
            self.synthesis_events.append({
                "operation": step.operation,
                "ok": bool(getattr(result, "ok", False)),
                "attempts": len(getattr(result, "attempts", []) or []),
            })
            if not getattr(result, "ok", False):
                raise ExecutorError(
                    f"synthesis failed for {step.operation!r} after "
                    f"{len(getattr(result, 'attempts', []) or [])} attempts"
                )
            skill = memory.get_skill(self.db, step.operation)
            if skill is None:
                raise ExecutorError(f"skill {step.operation!r} not registered after synthesis")

        fn = sandbox.compile_skill(skill["code"])
        kwargs = self._skill_kwargs(step, skill["contract"])
        try:
            result = sandbox.run_skill(fn, client=self.client, kwargs=kwargs,
                                       timeout_s=SKILL_TIMEOUT_S)
        except Exception:
            self._record_skill_outcome(step.operation, success=False)
            raise
        self._record_skill_outcome(step.operation, success=True)
        return result

    def _record_skill_outcome(self, operation: str, success: bool) -> None:
        """Update the skill's health counters and, when learning is on, quarantine it if its
        success rate has fallen below threshold (spec §7.1)."""
        memory.bump_skill_stats(self.db, operation, success)
        if self.learning_enabled:
            self._maybe_quarantine(operation)

    def _maybe_quarantine(self, operation: str) -> None:
        skill = memory.get_skill(self.db, operation)
        if skill is None or skill["status"] != "active":
            return
        conf = memory.skill_confidence(skill["uses"], skill["successes"])
        if skill["uses"] >= SKILL_MIN_SAMPLES and conf < SKILL_CONFIDENCE_THRESHOLD:
            memory.set_skill_status(self.db, operation, "quarantined")
            self.skill_health_events.append({
                "operation": operation, "event": "quarantined",
                "version": skill["version"], "confidence": round(conf, 2),
            })

    def _heal_quarantined(self, step: Step, skill: dict) -> dict:
        """Re-synthesise a quarantined skill (version+1) before it is ever run again. Returns
        the fresh active skill row, or raises so the failure surfaces through the §9 policy."""
        new_version = skill["version"] + 1
        if self.synthesizer is None or not self.learning_enabled:
            raise ExecutorError(f"skill {step.operation!r} is quarantined and healing is off")
        result = self.synthesizer(step, self.run_refs)   # reason -> build -> test -> re-register
        healed = bool(getattr(result, "ok", False))
        self.synthesis_events.append({
            "operation": step.operation, "ok": healed, "heal": True,
            "version": new_version,
            "attempts": len(getattr(result, "attempts", []) or []),
        })
        if not healed:
            raise ExecutorError(
                f"skill {step.operation!r} quarantined and re-synthesis failed after "
                f"{len(getattr(result, 'attempts', []) or [])} attempts"
            )
        memory.set_skill_version(self.db, step.operation, new_version)
        self.skill_health_events.append({
            "operation": step.operation, "event": "healed", "version": new_version,
        })
        return memory.get_skill(self.db, step.operation)

    def _skill_kwargs(self, step: Step, contract: dict | None) -> dict[str, Any]:
        """Build the skill's kwargs via the shared resolver — real run references outrank the
        planner's placeholder args — so the skill runs on the same inputs it was tested with."""
        contract = contract if isinstance(contract, dict) else {}
        return synthesizer.resolve_skill_kwargs(
            contract.get("inputs", {}), step.args, self.run_refs, contract.get("test_args", {})
        )

    # --- run loop ---------------------------------------------------------------

    def run(self, run_id: int, steps: list[Step]) -> list[StepResult]:
        """Execute the planned steps in order. Before each step, learned precondition rules
        inject prerequisite steps (e.g. labels.ensure) up front; a step that fails with a
        discoverable constraint (a 422/404 on a bare enrichment op) is recovered once by
        learning the rule, running the prerequisite, and retrying. Steps are numbered in
        EXECUTION order so injected prerequisites and retries read naturally in the report."""
        self.run_id = run_id
        self._fan_out_skip_labels = self._collect_fan_out_skip_labels(steps)
        self.run_refs = {}           # fresh within-run reference scope
        self.synthesis_events = []   # fresh per-run synthesis log
        self.preapplied_rules = []   # fresh per-run pre-applied-rule log
        self.skill_health_events = []  # fresh per-run skill quarantine/heal log
        self._results = []           # every executed / injected / skipped step result
        self._completed = []         # done mutations, flipped to rolled_back on a fatal stop
        self._meta = {}              # exec-seq -> (intent, operation, kind) for record_step
        self._exec_seq = 0
        self._fatal = False

        executed: set[int] = set()
        for step in steps:
            if self._fatal:
                break
            # pre-load learned preconditions: inject known prerequisites BEFORE the step
            for prereq in self._precondition_steps(step):
                if self._fatal:
                    break
                self._execute(prereq, planned=False)
            if self._fatal:
                break
            self._execute(step, planned=True)
            executed.add(id(step))

        # planned steps that never ran (after a fatal stop) -> skipped
        for step in steps:
            if id(step) not in executed:
                self._exec_seq += 1
                self._meta[self._exec_seq] = (step.intent, step.operation, step.kind)
                self._results.append(StepResult(seq=self._exec_seq, operation=step.operation,
                                                status="skipped"))

        self._results.sort(key=lambda r: r.seq)
        for r in self._results:
            intent, operation, kind = self._meta[r.seq]
            memory.record_step(self.db, run_id, r.seq, intent, operation, kind,
                               r.status, r.latency_ms, r.error, r.resolution)
        return self._results

    def _execute(self, step: Step, planned: bool) -> None:
        """Dispatch one step (planned or injected), classify a failure, and — for a planned
        step hitting a discoverable constraint — attempt the learn-and-retry recovery."""
        res, inverses, error = self._dispatch_recorded(step)
        if res.status == "done":
            self._results.append(res)
            if inverses:
                self._completed.append(res)
            return

        self._results.append(res)                # the failure is always visible in the report
        if planned and self._maybe_recover(step, error, res):
            return
        if not planned:                          # an injected prerequisite failing is non-fatal
            res.resolution = res.resolution or "injected prerequisite failed; dependent step may fail"
            return
        if operations.is_enrichment(step.operation):
            res.resolution = res.resolution or "enrichment op failed; primary kept, continuing"
            return
        res.resolution = "fatal failure; rolled back this run's completed mutations"
        self._rollback(self.run_id)
        for cr in self._completed:
            cr.status = "rolled_back"
        self._fatal = True

    def _dispatch_recorded(self, step: Step):
        """Dispatch one step; time it; bump op_stats; journal any inverses. Returns
        (StepResult, inverses, error) — error is the raised exception, or None on success."""
        self._exec_seq += 1
        seq = self._exec_seq
        self._meta[seq] = (step.intent, step.operation, step.kind)
        t0 = time.perf_counter()
        self.client.journal = []
        try:
            self._dispatch(step)
        except Exception as e:  # noqa: BLE001 — classified by the caller, not swallowed
            latency = int((time.perf_counter() - t0) * 1000)
            memory.bump_op_stats(self.db, step.operation, success=False, latency_ms=latency)
            return (StepResult(seq=seq, operation=step.operation, status="failed",
                               latency_ms=latency, error=str(e)), [], e)
        latency = int((time.perf_counter() - t0) * 1000)
        memory.bump_op_stats(self.db, step.operation, success=True, latency_ms=latency)
        inverses = list(self.client.journal or [])
        for inv in inverses:
            memory.journal_append(self.db, self.run_id, seq, inv)
        res = StepResult(seq=seq, operation=step.operation, status="done", latency_ms=latency)
        return res, inverses, None

    # --- constraint pre-loading + learn-and-retry (spec §8, §9) -----------------

    def _precondition_steps(self, step: Step) -> list[Step]:
        """Prerequisite steps to inject before `step`, derived from its learned precondition
        rules — this is the cross-run/cross-instruction transfer: a rule learned the hard way
        once is pre-applied for free on every later run that touches the same operation."""
        out: list[Step] = []
        if not self.learning_enabled:            # cold baseline: no rule pre-loading
            return out
        for rule in memory.rules_for(self.db, step.operation):
            if rule["rule_type"] != "precondition":
                continue
            prereq = self._build_prerequisite(step, rule["detail"])
            if prereq is not None:
                out.append(prereq)
                self.preapplied_rules.append({
                    "operation": step.operation,
                    "action": rule["detail"].get("action"),
                    "param": rule["detail"].get("param"),
                    "learned_in_run": rule["learned_in_run"],
                })
        return out

    def _build_prerequisite(self, step: Step, detail: dict) -> Step | None:
        """Turn a precondition rule (e.g. {"action":"labels.ensure","param":"label"}) into a
        concrete prerequisite step carrying the subject value from the dependent step's args."""
        prereq_op = detail.get("action")
        param = detail.get("param")
        if not prereq_op or not param:
            return None
        subject = step.args.get(param)
        if subject is None:                      # planner may have used the plural form
            labels = step.args.get("labels")
            if labels:
                subject = labels[0]
        if subject is None:
            return None
        return Step(seq=step.seq, intent=f"ensure prerequisite for {step.operation}",
                    operation=prereq_op, kind="api", args={param: subject})

    def _evict_stale_ref(self, rule: dict, step: Step) -> None:
        """Drop the ref_cache entry for a precondition's subject, so the prerequisite *.ensure
        re-resolves it live. The cached id only reaches here after a 422 proved it unusable
        (e.g. a milestone number that was deleted)."""
        action = rule.get("action", "")
        kind = {"labels.ensure": "label", "milestones.ensure": "milestone"}.get(action)
        if kind is None:
            return
        subject = step.args.get(rule.get("param"))
        if subject is None:
            labels = step.args.get("labels")
            subject = labels[0] if labels else None
        if subject is not None:
            memory.del_ref(self.db, f"{kind}:{subject}")

    def _maybe_recover(self, step: Step, error, failed_res: StepResult) -> bool:
        """A planned step failed; if it is a discoverable constraint (a 422/404 on a bare
        enrichment op), learn the precondition rule, inject + run the prerequisite (synthesised
        on first use), and retry the step once. Returns True iff fully handled here (the retry
        succeeded, or the recovery turned fatal); False to fall through to the §9 class policy."""
        if not self.learning_enabled:            # cold baseline: no learn-and-retry recovery
            return False
        rule = self._extract_constraint_rule(step.operation, error)
        if rule is None:
            return False
        prereq_op = rule["action"]
        # only attempt recovery if we can actually satisfy the prerequisite
        if memory.get_skill(self.db, prereq_op) is None and self.synthesizer is None:
            return False
        if not self._has_rule(step.operation, "precondition", rule):
            memory.add_rule(self.db, step.operation, "precondition", rule,
                            learned_in_run=self.run_id)
        failed_res.resolution = (
            f"missing prerequisite (HTTP {getattr(error, 'status_code', '?')}); learned "
            f"precondition rule -> {prereq_op}; pre-applied it and retried"
        )
        # the cached id may be STALE — the underlying object was deleted on the platform, which
        # is exactly why this 422'd. Evict it so the injected *.ensure re-resolves against the
        # live repo instead of short-circuiting to the dead id again (self-heal, not a 422 loop).
        self._evict_stale_ref(rule, step)
        prereq = self._build_prerequisite(step, rule)
        if prereq is None:
            return False
        self._execute(prereq, planned=False)
        if self._fatal:
            return True
        last = self._results[-1]
        if not (last.operation == prereq_op and last.status == "done"):
            return False                         # couldn't satisfy the precondition
        res, inverses, _ = self._dispatch_recorded(step)   # retry the original step once
        res.resolution = f"retry after pre-applied {prereq_op}"
        self._results.append(res)
        if res.status == "done":
            if inverses:
                self._completed.append(res)
            return True
        return False

    @staticmethod
    def _extract_constraint_rule(op: str, error) -> dict | None:
        """A discoverable precondition: a bare enrichment op failing with a 422/404 because its
        subject (label/milestone) must exist first. Returns the rule detail, or None."""
        if not isinstance(error, GitHubError) or error.status_code not in (404, 422):
            return None
        if op == "issues.add_label":
            return {"action": "labels.ensure", "param": "label"}
        if op == "issues.set_milestone":
            return {"action": "milestones.ensure", "param": "milestone"}
        return None

    def _has_rule(self, operation: str, rule_type: str, detail: dict) -> bool:
        return any(r["rule_type"] == rule_type and r["detail"] == detail
                   for r in memory.rules_for(self.db, operation))

    @staticmethod
    def _ensure_ref_value(result, kind: str, subject):
        """Pull the resolved id/number out of a *.ensure skill's result for the ref_cache —
        a milestone caches its number (needed to set it), a label its id; fall back to the
        subject name itself so the cache at least records that it now exists."""
        if isinstance(result, dict):
            keys = ("number",) if kind == "milestone" else ("id", "node_id")
            for k in keys + ("name", "title"):
                if result.get(k) is not None:
                    return result[k]
        if isinstance(result, (str, int)):
            return result
        return subject

    # --- rollback ---------------------------------------------------------------

    def _rollback(self, run_id: int) -> None:
        """Replay this run's recorded inverses in reverse; do not journal the undo ops."""
        self.client.journal = None
        for entry in reversed(memory.journal_for(self.db, run_id)):
            inv = entry["inverse"]
            self._apply_inverse(inv)
            memory.mark_applied(self.db, entry["id"])

    def _apply_inverse(self, inv) -> None:
        if inv.method == "rest_patch":
            self.client.rest_patch(inv.path, json=inv.body)
        elif inv.method == "rest_delete":
            self.client.rest_delete(inv.path, json=inv.body or None)
        elif inv.method == "rest_post":
            self.client.rest_post(inv.path, json=inv.body)
