"""Request and response models for the fixed API contract (guide section 6)."""
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AnswerType = Literal["allocation", "scenario", "brief", "infeasible", "clarification_needed", "refused"]


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cost: float = Field(0.7, ge=0, le=1)
    carbon: float = Field(0.3, ge=0, le=1)
    lateness: float | None = Field(None, ge=0, le=1)

    @field_validator("carbon")
    @classmethod
    def _sum(cls, v, info):
        if info.data.get("cost", 0) + v <= 0:
            raise ValueError("cost and carbon weights cannot both be zero")
        return v


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request: str = Field(..., min_length=1, max_length=2000)
    weights: Weights | None = None
    scenario: dict[str, Any] | None = Field(None, description='e.g. {"type": "capacity_loss", "plant": "B", '
                                            '"capacity_loss_pct": 30, "from_date": "2026-10-19", "to_date": "2026-11-01"}')
    policy: Literal["never_late", "allow_late"] | None = None
    planning_start: date | None = None


class Recommendation(BaseModel):
    action: str
    rationale: str
    evidence_refs: list[str]
    confidence: Literal["high", "medium", "low"]
    estimated_impact: str
    requires_approval: bool


class AnalyzeResponse(BaseModel):
    run_id: str
    answer_type: AnswerType
    summary: str
    allocation: list[dict] = []
    kpis: dict | None = None
    tradeoff: list[dict] | None = None
    weights_used: dict
    validator: dict | None = None
    recommendations: list[Recommendation] = []
    assumptions: list[str] = []
    limitations: list[str] = []
    # extensions used by the UI; all come from tool output
    status: str | None = None
    unmet: list[dict] = []
    comparison: dict | None = None
    explanation: dict | None = None
    brief: str | None = None
    clarification: dict | None = None
    number_check: dict | None = None
    solver: dict | None = None
    parent_run_id: str | None = None
    baseline_kpis: dict | None = None
