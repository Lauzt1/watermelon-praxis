"""Restricted compile + run for synthesised skills (spec §7 step 3).

A skill is LLM-generated code we are about to execute, so it gets two guards:

  1. A *static* AST walk that rejects the escape hatches a generated `skill` could use
     to leave the in-process namespace — `import`/`from-import`, `open`/`eval`/`exec`/
     `__import__`, any banned name (e.g. `socket`, `os`), and dunder attribute walks
     (`().__class__.__bases__`). This is a structural check, not a string match, so
     `"# import os"` in a comment is fine but `__cl` + `ass__` tricks still can't form a
     real dunder access node.
  2. A restricted execution namespace: only a small whitelist of builtins is exposed and
     the only injected object is the `client`. Combined with the static guard there is no
     reachable path to the filesystem, network, or the import machinery.

`run_skill` adds a thread watchdog so a runaway loop raises `TimeoutError` instead of
hanging the agent (thread-based, not signal-based, so it works on Windows).
"""
from __future__ import annotations

import ast
import builtins
import threading
from typing import Any, Callable

# Names that must never appear in a skill — the import machinery, raw I/O, dynamic eval,
# attribute pokers, and the obvious stdlib network/process modules. `import` itself is
# rejected structurally below, so these only catch a bare reference that slipped through.
_BANNED_NAMES: frozenset[str] = frozenset({
    "open", "eval", "exec", "compile", "__import__", "input",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "os", "sys", "socket", "subprocess", "importlib", "builtins", "breakpoint",
})

# Builtins a pure transform legitimately needs. Deliberately small; extends the plan's
# core list with the obvious helpers a grouping/table skill uses (tuple/reversed/any/all/
# map/filter/abs/round/repr) — all pure, none reach I/O.
_WHITELIST: tuple[str, ...] = (
    "len", "range", "sorted", "sum", "min", "max", "dict", "list", "set", "tuple",
    "str", "int", "float", "bool", "enumerate", "zip", "reversed", "abs", "round",
    "any", "all", "map", "filter", "repr", "isinstance",
)
_ALLOWED_BUILTINS: dict[str, Any] = {name: getattr(builtins, name) for name in _WHITELIST}


class SandboxError(ValueError):
    """A skill was rejected by the static guard. Subclasses ValueError so callers (and
    the tests) can catch the broad shape while we still carry a precise message."""


def _validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SandboxError("imports are not allowed in a skill")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SandboxError(f"dunder attribute access is not allowed: .{node.attr}")
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise SandboxError(f"use of banned name is not allowed: {node.id}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BANNED_NAMES:
                raise SandboxError(f"call to banned function is not allowed: {func.id}")


def compile_skill(code: str, name: str = "skill") -> Callable:
    """Validate, then compile + exec `code` in a restricted namespace; return the `skill`
    function object. Raises SandboxError (a ValueError) on any forbidden construct, a
    syntax error, or a missing/non-callable `skill`."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxError(f"syntax error in skill: {e}") from e
    _validate(tree)
    sandbox_globals: dict[str, Any] = {"__builtins__": _ALLOWED_BUILTINS}
    compiled = compile(tree, "<skill>", "exec")
    exec(compiled, sandbox_globals)  # noqa: S102 — code is AST-validated + builtins-restricted
    fn = sandbox_globals.get(name)
    if not callable(fn):
        raise SandboxError(f"skill source defines no callable {name!r}")
    return fn


def run_skill(fn: Callable, client: Any, kwargs: dict[str, Any], timeout_s: float = 5.0) -> Any:
    """Call `fn(client, **kwargs)` on a daemon worker thread; raise TimeoutError if it
    overruns `timeout_s`, and re-raise any exception the skill itself threw."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn(client, **kwargs)
        except BaseException as e:  # noqa: BLE001 — surfaced to the caller unchanged
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"skill exceeded {timeout_s}s timeout")
    if "error" in box:
        raise box["error"]
    return box.get("value")
