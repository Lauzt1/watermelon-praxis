"""Capability synthesis (spec §7) — turn an operation the executor has no skill for into
a tested, registered skill, at runtime.

Five steps, exactly as the spec lays them out:
  1. **Reason a contract** — the workhorse LLM returns a Pydantic-validated `SkillContract`
     (name, typed inputs/output, the primitives it may use, safe `test_args`).
  2. **Generate** a single ``def skill(client, **kwargs)`` function (raw string, no JSON).
  3. **Compile** it in the restricted sandbox.
  4. **Test, strategy by kind** — a pure ``compute.*`` transform runs in-memory against the
     contract's sample `test_args` (no API); an effectful op runs against the **real** client
     with `test_args`, its inverse captured in the journal and **replayed to self-clean**.
  5. **Register** (`memory.put_skill`) on success; after **N=3** failed code attempts return a
     structured failure carrying the contract + every attempt's error — no fake success.

Signature reconciliation: the executor calls ``self.synthesizer(step)``; the canonical
function here is ``synthesize(gap, client, db, llm)`` (``db`` is the sqlite connection — the
plan's "memory" — writes go through the `memory` module). The orchestrator/CLI bridges the
two with a thin closure ``lambda step: synthesize(step, client, db, llm)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import memory, operations
from .models import InverseOp, SkillContract, Step
from .sandbox import compile_skill, run_skill

MAX_ATTEMPTS = 3
TEST_TIMEOUT_S = 8.0
# The workhorse is a REASONING model: its chain-of-thought counts against max_tokens and
# can be several thousand tokens before the actual answer. Budget generously or `.content`
# comes back empty (the spec's reasoning-model gotcha). Contract is small JSON; code is larger.
CONTRACT_MAX_TOKENS = 6000
CODE_MAX_TOKENS = 8000

# The only platform primitives a skill may compose (spec §7 step 2). Pinned here; the
# prompt body around it is tuned against the live model.
ALLOWED_PRIMITIVES = ("rest_get", "rest_post", "rest_patch", "rest_delete", "graphql")


@dataclass
class SynthesisResult:
    """Outcome of one synthesis. On success `fn` is the compiled callable and the skill is
    persisted; on failure `attempts` holds each attempt's error and nothing is registered."""
    ok: bool
    operation: str
    contract: SkillContract | None = None
    code: str | None = None
    fn: Callable | None = None
    attempts: list[str] = field(default_factory=list)


class SynthesisError(Exception):
    """Raised inside a single attempt (compile or test failure); collected, not fatal."""


# --- prompts (bodies tuned live; the schema + allowed-primitive list are fixed) --------

def _contract_messages(gap: Step, client: Any) -> list[dict[str, str]]:
    pure = operations.is_compute(gap.operation)
    kind = "a PURE in-process transform (calls NO API primitives)" if pure else \
        "an EFFECTFUL GitHub operation (composes API primitives)"
    system = (
        "You design capabilities for a GitHub automation agent. Output ONLY a JSON object "
        "matching this SkillContract schema:\n"
        "  name: string (MUST equal the given operation)\n"
        "  inputs: object mapping kwarg name -> a short type description\n"
        "  output: string describing what the skill returns\n"
        "  primitives: array, a subset of "
        f"{list(ALLOWED_PRIMITIVES)} the skill will call (EMPTY for a pure transform)\n"
        "  test_args: object of safe kwargs to test the skill with. For an EFFECTFUL skill "
        "these hit a throwaway repo, so use clearly-temporary values (e.g. names prefixed "
        "'praxis-synth-test'). For a PURE transform, provide small inline sample data.\n"
    )
    user = (
        f"operation: {gap.operation}\n"
        f"intent: {gap.intent}\n"
        f"kind: {kind}\n"
        f"repo: {getattr(client, 'repo', '')}\n"
        f"planner args (context): {gap.args}\n"
        "Design the SkillContract now."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _code_messages(contract: SkillContract, client: Any, prior_error: str | None) -> list[dict[str, str]]:
    system = (
        "Write ONE Python function with EXACTLY this signature:\n"
        "    def skill(client, **kwargs):\n"
        "Rules: use ONLY these via the injected client object — "
        f"{', '.join('client.' + p for p in ALLOWED_PRIMITIVES)} "
        "(GET takes params=..., the others take json=...). "
        "NO imports, NO open/eval/exec, NO file or network access except through `client`, "
        "NO dunder attribute access. The kwargs are exactly the contract inputs. "
        "Return the result described by the contract. "
        # The real GitHub REST shapes the model must not get wrong — labels/assignees/
        # milestone are OBJECTS, not strings, on a fetched issue.
        "IMPORTANT — real GitHub issue JSON shape: an issue is a dict with keys including "
        "'number' (int), 'title' (str), 'html_url' (str), 'assignee' (object|null), and "
        "'labels' (a LIST OF OBJECTS, each like {'name': 'bug', ...}). So to group by label "
        "use label['name'] (a string) as the key — NEVER the label object itself. "
        "Output ONLY the function source code — no markdown fences, no prose."
    )
    user = (
        f"name: {contract.name}\n"
        f"inputs: {contract.inputs}\n"
        f"output: {contract.output}\n"
        f"primitives allowed for this skill: {contract.primitives}\n"
        f"repo: {getattr(client, 'repo', '')}\n"
    )
    if prior_error:
        user += f"\nThe previous attempt failed with:\n{prior_error}\nFix it."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _strip_fences(code: str) -> str:
    """Tolerate a model that wraps code in ```python ... ``` despite being told not to."""
    text = code.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]                       # drop opening fence (``` or ```python)
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]                  # drop closing fence
        text = "\n".join(lines).strip()
    return text


# --- skill testing, strategy by kind --------------------------------------------------

def _replay_inverse(client: Any, inv: InverseOp) -> None:
    if inv.method == "rest_patch":
        client.rest_patch(inv.path, json=inv.body)
    elif inv.method == "rest_delete":
        client.rest_delete(inv.path, json=inv.body or None)
    elif inv.method == "rest_post":
        client.rest_post(inv.path, json=inv.body)


# input names a transform conventionally uses for "the list it operates on"
_LIST_INPUT_ALIASES = ("issues", "items", "rows", "data", "results")


def resolve_skill_kwargs(inputs, args: dict | None, run_refs: dict | None,
                         test_args: dict | None = None) -> dict[str, Any]:
    """Build a skill's kwargs from, in priority: the run's REAL references (e.g. the actual
    issues.list output), the planner's literal args, then the contract's sample test_args.

    Used identically at synthesis-test time and at execution time, so a skill is tested with
    EXACTLY what it will be run with. The priority matters: the planner emits placeholders
    like "$steps.1.result" for data it can't know at plan time, so real run data must win —
    otherwise the skill iterates the placeholder string and crashes on real data only."""
    args = args or {}
    run_refs = run_refs or {}
    test_args = test_args or {}
    kwargs: dict[str, Any] = {}
    for name in inputs:
        if name in run_refs:
            kwargs[name] = run_refs[name]
        elif name in _LIST_INPUT_ALIASES and "issues" in run_refs:
            kwargs[name] = run_refs["issues"]
        elif name in args:
            kwargs[name] = args[name]
        elif name in test_args:
            kwargs[name] = test_args[name]
    return kwargs


def _test_skill(fn: Callable, gap: Step, contract: SkillContract, client: Any,
                run_refs: dict | None = None) -> Any:
    """Pure compute -> run in-memory (no client) against the SAME kwargs execution will use
    (real upstream data when available). Effectful -> run against the real client, capture
    its inverse ops in the journal, then replay them to self-clean."""
    if operations.is_compute(gap.operation):
        kwargs = resolve_skill_kwargs(contract.inputs, gap.args, run_refs, contract.test_args)
        result = run_skill(fn, client=None, kwargs=kwargs, timeout_s=TEST_TIMEOUT_S)
        if result is None:
            raise SynthesisError("pure transform returned None on its test args")
        return result

    saved = getattr(client, "journal", None)
    client.journal = []
    captured: list[InverseOp] = []
    try:
        result = run_skill(fn, client=client, kwargs=contract.test_args, timeout_s=TEST_TIMEOUT_S)
        captured = list(client.journal or [])
    finally:
        client.journal = None                   # don't journal the cleanup itself
        for inv in reversed(captured):
            try:
                _replay_inverse(client, inv)
            except Exception:  # noqa: BLE001 — best-effort self-clean; the test verdict stands
                pass
        client.journal = saved
    return result


# --- the loop -------------------------------------------------------------------------

def synthesize(gap: Step, client: Any, db, llm, run_refs: dict | None = None,
               max_attempts: int = MAX_ATTEMPTS) -> SynthesisResult:
    """Reason a contract once, then up to `max_attempts` generate->compile->test cycles.
    `run_refs` (the executor's within-run references) lets a pure transform be tested
    against the run's real upstream data. Register on the first success; otherwise return a
    structured failure."""
    workhorse = llm.config.model_workhorse
    try:
        contract: SkillContract = llm.complete(
            _contract_messages(gap, client), model=workhorse,
            schema=SkillContract, max_tokens=CONTRACT_MAX_TOKENS, json_mode=True,
        )
    except Exception as e:  # noqa: BLE001 — a bad/empty contract is a clean failure, not a crash
        return SynthesisResult(ok=False, operation=gap.operation,
                               attempts=[f"contract: {type(e).__name__}: {e}"])
    # the contract name is the join key the executor looks up by — pin it to the operation
    if contract.name != gap.operation:
        contract = contract.model_copy(update={"name": gap.operation})

    attempts: list[str] = []
    prior_error: str | None = None
    for i in range(max_attempts):
        try:
            raw = llm.complete(
                _code_messages(contract, client, prior_error), model=workhorse,
                max_tokens=CODE_MAX_TOKENS, json_mode=False,
            )
            code = _strip_fences(raw)
            fn = compile_skill(code)
            _test_skill(fn, gap, contract, client, run_refs)
        except Exception as e:  # noqa: BLE001 — every attempt error is reported, not raised
            prior_error = f"{type(e).__name__}: {e}"
            attempts.append(f"attempt {i + 1}: {prior_error}")
            continue
        memory.put_skill(db, gap.operation, contract, code, status="active")
        return SynthesisResult(ok=True, operation=gap.operation, contract=contract,
                               code=code, fn=fn, attempts=attempts)

    return SynthesisResult(ok=False, operation=gap.operation, contract=contract,
                           code=None, fn=None, attempts=attempts)
