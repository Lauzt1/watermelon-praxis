import pytest
from pydantic import ValidationError
from praxis.models import Step, Plan, SkillContract, StepResult, Report, InverseOp

def test_step_requires_known_kind():
    Step(seq=1, intent="make issue", operation="issues.create", kind="api", args={})
    with pytest.raises(ValidationError):
        Step(seq=1, intent="x", operation="issues.create", kind="bogus", args={})

def test_plan_roundtrips_json():
    p = Plan(signature="sig", steps=[Step(seq=1, intent="x", operation="issues.create", kind="api", args={})])
    assert Plan.model_validate_json(p.model_dump_json()) == p

def test_report_serialises_metrics():
    r = Report(instruction="i", status="ok", steps=[], metrics={"api_calls": 2},
               memory_delta={}, synthesis_events=[])
    assert "api_calls" in r.model_dump_json()
