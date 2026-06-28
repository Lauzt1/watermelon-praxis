from praxis import memory
from praxis.models import SkillContract


def _register(db, name="compute.demo"):
    c = SkillContract(name=name, inputs={"items": "list"}, output="sorted",
                      primitives=[], test_args={"items": [3, 1, 2]})
    memory.put_skill(db, name, c, "def skill(client, items):\n    return sorted(items)")
    return name


def test_bump_skill_stats_counts_success_and_failure(db):
    name = _register(db)
    memory.bump_skill_stats(db, name, success=True)
    memory.bump_skill_stats(db, name, success=False)
    s = memory.get_skill(db, name)
    assert (s["uses"], s["successes"], s["failures"]) == (2, 1, 1)


def test_bump_skill_stats_missing_skill_is_noop(db):
    memory.bump_skill_stats(db, "compute.nope", success=False)  # must not raise


def test_set_skill_status_and_version(db):
    name = _register(db)
    memory.set_skill_status(db, name, "quarantined")
    memory.set_skill_version(db, name, 2)
    s = memory.get_skill(db, name)
    assert s["status"] == "quarantined" and s["version"] == 2


def test_skill_confidence_math():
    assert memory.skill_confidence(0, 0) == 1.0
    assert memory.skill_confidence(3, 0) == 0.0
    assert memory.skill_confidence(4, 2) == 0.5


from praxis.executor import Executor
from praxis.synthesizer import SynthesisResult
from praxis.models import Step


def test_maybe_quarantine_flips_unhealthy_skill(db):
    name = _register(db)
    memory.bump_skill_stats(db, name, True)
    memory.bump_skill_stats(db, name, False)
    memory.bump_skill_stats(db, name, False)        # 3 uses, 1 ok -> conf 0.33 < 0.5
    Executor(db, client=None)._maybe_quarantine(name)
    assert memory.get_skill(db, name)["status"] == "quarantined"


def test_maybe_quarantine_leaves_healthy_skill_active(db):
    name = _register(db)
    for _ in range(5):
        memory.bump_skill_stats(db, name, True)
    Executor(db, client=None)._maybe_quarantine(name)
    assert memory.get_skill(db, name)["status"] == "active"


def test_quarantined_skill_is_resynthesised_on_dispatch(db):
    name = "compute.demo"
    bad = SkillContract(name=name, inputs={"items": "list"}, output="sorted",
                        primitives=[], test_args={"items": [3, 1, 2]})
    memory.put_skill(db, name, bad,
                     "def skill(client, **kwargs):\n    raise RuntimeError('drift')",
                     status="quarantined")

    def stub_synth(step, refs=None):
        good = SkillContract(name=step.operation, inputs={"items": "list"}, output="sorted",
                             primitives=[], test_args={"items": [3, 1, 2]})
        memory.put_skill(db, step.operation, good,
                         "def skill(client, items):\n    return sorted(items)", status="active")
        return SynthesisResult(ok=True, operation=step.operation)

    ex = Executor(db, client=None, synthesizer=stub_synth)
    ex.run_refs = {"items": [3, 1, 2]}
    result = ex._dispatch_skill(Step(seq=1, intent="sort", operation=name, kind="compute", args={}))
    assert result == [1, 2, 3]                       # ran the HEALED code, not the broken code
    healed = memory.get_skill(db, name)
    assert healed["status"] == "active" and healed["version"] == 2
    assert any(e["event"] == "healed" for e in ex.skill_health_events)


from praxis.reporter import render
from praxis.models import Report


def test_report_renders_skill_health():
    r = Report(instruction="i", status="ok", steps=[],
               metrics={"api_calls": 1, "llm_calls": 0, "wall_ms": 1, "failure_count": 0},
               memory_delta={"skill_health": [
                   {"operation": "compute.demo", "event": "healed", "version": 2}]},
               synthesis_events=[])
    assert "Skill health: compute.demo healed -> v2" in render(r)


def test_render_memory_shows_skill_confidence(db):
    from praxis.main import render_memory
    _register(db)
    assert "conf=1.00" in render_memory(db)
