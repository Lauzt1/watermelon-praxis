from typing import Any, Literal
from pydantic import BaseModel

class Step(BaseModel):
    seq: int
    intent: str
    operation: str
    kind: Literal["api", "compute"]
    args: dict[str, Any] = {}

class Plan(BaseModel):
    signature: str
    steps: list[Step]

class SkillContract(BaseModel):
    name: str                      # e.g. "compute.group_by_label"
    inputs: dict[str, str]         # name -> type description
    output: str
    primitives: list[str]          # subset of adapter primitives the skill may call
    test_args: dict[str, Any]      # safe args to test against the real API

class StepResult(BaseModel):
    seq: int
    operation: str
    status: Literal["done", "failed", "rolled_back", "skipped"]
    latency_ms: int = 0
    error: str | None = None
    resolution: str | None = None   # what the agent decided/did about it

class InverseOp(BaseModel):
    method: Literal["rest_delete", "rest_patch", "rest_post"]
    path: str
    body: dict[str, Any] = {}

class Report(BaseModel):
    instruction: str
    status: Literal["ok", "partial", "failed"]
    steps: list[StepResult]
    metrics: dict[str, Any]          # api_calls, llm_calls, wall_ms, failure_count
    memory_delta: dict[str, Any]     # what was learned/cached this run
    synthesis_events: list[dict[str, Any]] = []
