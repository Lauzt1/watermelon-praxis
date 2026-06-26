"""Sandbox tests — synthesised skill code is compiled under a static AST guard and run
under a soft timeout. The guard must reject the escape hatches a generated skill could
use to break out of the in-process namespace (imports, raw I/O, eval/exec, dunder walks),
and accept a clean transform. Run is thread-watchdog'd so an infinite loop can't hang us.
"""
import pytest

from praxis.sandbox import compile_skill, run_skill


# --- rejections (each via the AST walk, not a string match) ---------------------

def test_sandbox_rejects_import():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    import os\n    return 1")


def test_sandbox_rejects_from_import():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    from os import path\n    return 1")


def test_sandbox_rejects_open():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    return open('/etc/passwd').read()")


def test_sandbox_rejects_eval():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    return eval('1+1')")


def test_sandbox_rejects_exec():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    exec('x=1')\n    return 1")


def test_sandbox_rejects_dunder_import():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    return __import__('os')")


def test_sandbox_rejects_socket():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    return socket")


def test_sandbox_rejects_dunder_escape():
    with pytest.raises(ValueError):
        compile_skill("def skill(client):\n    return ().__class__.__bases__")


def test_sandbox_rejects_missing_skill_function():
    with pytest.raises(ValueError):
        compile_skill("def helper(client):\n    return 1")


# --- acceptance + execution -----------------------------------------------------

def test_sandbox_runs_clean_skill():
    fn = compile_skill("def skill(client, items):\n    return sorted(items)")
    assert run_skill(fn, client=None, kwargs={"items": [3, 1, 2]}, timeout_s=2) == [1, 2, 3]


def test_sandbox_skill_may_use_injected_client():
    fn = compile_skill("def skill(client, n):\n    return client.double(n)")

    class C:
        def double(self, n):
            return n * 2

    assert run_skill(fn, client=C(), kwargs={"n": 21}, timeout_s=2) == 42


def test_sandbox_run_raises_on_timeout():
    fn = compile_skill("def skill(client):\n    while True:\n        pass")
    with pytest.raises(TimeoutError):
        run_skill(fn, client=None, kwargs={}, timeout_s=0.2)


def test_sandbox_propagates_skill_exception():
    fn = compile_skill("def skill(client):\n    return [][1]")
    with pytest.raises(IndexError):
        run_skill(fn, client=None, kwargs={}, timeout_s=2)
