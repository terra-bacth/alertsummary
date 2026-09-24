"""Planner agent: turns a correlated incident into an investigation plan."""

from __future__ import annotations

import logging

from tsagent.events import emit
from tsagent.graph.context import affected_nodes, incident_header
from tsagent.graph.deps import GraphDeps
from tsagent.graph.schemas import InvestigationPlan
from tsagent.graph.state import IncidentState
from tsagent.llm import LLMError
from tsagent.runbooks import runbook_hints

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are an SRE planning the investigation of an OpenStack (RHOSP) alarm incident. "
    "Prometheus metrics are available now; SSH access to nodes is possible but expensive, so "
    "plan metric checks first. Be specific and brief."
)


def _fallback_plan(state: IncidentState) -> InvestigationPlan:
    """Used when the LLM is unreachable — a generic plan still beats no investigation."""
    nodes = affected_nodes(state) or ["the affected node"]
    return InvestigationPlan(
        hypotheses=["Unable to reach the planning model; running a generic health sweep."],
        checks=[
            {"goal": f"Is {nodes[0]} up and scraping?", "metric_hint": "up"},
            {"goal": "Filesystem usage on the affected node", "metric_hint": "node_filesystem_avail_bytes"},
            {"goal": "CPU and load on the affected node", "metric_hint": "node_load5"},
            {"goal": "Memory pressure on the affected node", "metric_hint": "node_memory_MemAvailable_bytes"},
        ],
    )


async def planner_node(state: IncidentState, deps: GraphDeps) -> dict:
    await emit(
        deps.events,
        type="agent.started",
        status="running",
        incident_id=state.get("incident_id", ""),
        cluster=state.get("cluster", ""),
        agent="planner",
        step="plan",
        title="Planner thinking",
        detail="Building the investigation plan and hypotheses.",
    )

    max_checks = deps.app_config.investigation.max_promql_queries
    alarm_names = sorted({a.get("name", "") for a in state.get("alarms") or []})
    hints = runbook_hints(alarm_names)

    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f"{incident_header(state)}\n\n"
                f"{hints}\n"
                f"Produce at most {max_checks} metric checks and up to 3 ranked hypotheses.\n"
                'Reply with JSON only: {"hypotheses": ["..."], '
                '"checks": [{"goal": "...", "metric_hint": "..."}]}'
            ),
        },
    ]

    feedback = (state.get("reflections") or [])
    last = feedback[-1] if feedback else None
    if last and last.get("step") == "plan" and last.get("feedback"):
        messages.append(
            {"role": "user", "content": f"Revise the plan. Reviewer feedback: {last['feedback']}"}
        )

    try:
        plan = await deps.gateway.complete_json("planner", messages, InvestigationPlan)
    except LLMError as exc:
        logger.error("planner failed, using fallback plan: %s", exc)
        await emit(
            deps.events,
            type="agent.fallback",
            status="warn",
            incident_id=state.get("incident_id", ""),
            cluster=state.get("cluster", ""),
            agent="planner",
            step="plan",
            title="Planner offline — generic sweep used",
            detail=f"Planner LLM unavailable ({exc}); a generic health sweep was used.",
        )
        return {
            "plan": _fallback_plan(state).model_dump(),
            "caveats": [f"Planner LLM unavailable ({exc}); a generic health sweep was used."],
        }

    plan.checks = plan.checks[:max_checks]
    logger.info(
        "plan ready: %d hypotheses, %d checks", len(plan.hypotheses), len(plan.checks)
    )
    await emit(
        deps.events,
        type="plan.completed",
        status="ok",
        incident_id=state.get("incident_id", ""),
        cluster=state.get("cluster", ""),
        agent="planner",
        step="plan",
        title=f"Plan ready — {len(plan.checks)} checks",
        detail=f"{len(plan.hypotheses)} hypothesis·es ranked, {len(plan.checks)} metric checks queued.",
        data={"plan": plan.model_dump()},
    )
    return {"plan": plan.model_dump()}
