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
