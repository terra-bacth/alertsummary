"""Structured outputs each agent must produce.

Every LLM step is validated against one of these before the graph acts on it, so a model that
drifts into prose fails loudly at the parse boundary instead of quietly poisoning the report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MetricCheck(BaseModel):
    goal: str = Field(description="What this check should establish, in one line")
    metric_hint: str = Field(default="", description="Metric or exporter to look at, if known")


class InvestigationPlan(BaseModel):
    hypotheses: list[str] = Field(default_factory=list)
    checks: list[MetricCheck] = Field(default_factory=list)


class GeneratedQuery(BaseModel):
    goal: str
    promql: str
    range_query: bool = False


class QueryBatch(BaseModel):
    queries: list[GeneratedQuery] = Field(default_factory=list)


class Reflection(BaseModel):
    verdict: Literal["OK", "RETRY"]
    feedback: str = ""


class Decision(BaseModel):
    decision: Literal["sufficient", "need_node_access"]
    reason: str = ""
    target_nodes: list[str] = Field(default_factory=list)


class NodeCommand(BaseModel):
    node: str = ""
    command: str = ""
    rationale: str = ""
    done: bool = Field(default=False, description="True when no further command is needed")


class RCA(BaseModel):
    root_cause: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    recommendations: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    timeline: list[str] = Field(default_factory=list)
