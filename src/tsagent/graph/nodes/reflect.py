"""Reflection: the self-correction checkpoint between investigation steps.

One generic node, parameterized by the step it critiques. Retries are capped and counted in
state — a reflection loop that can run forever is worse than one that occasionally accepts
mediocre evidence, so exhausting the budget proceeds with a recorded caveat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from tsagent.events import emit
from tsagent.graph.context import evidence_digest, incident_header
from tsagent.graph.deps import GraphDeps
from tsagent.graph.schemas import Reflection
from tsagent.graph.state import IncidentState
from tsagent.llm import LLMError

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are Tony Tony Chopper, the Straw Hat doctor. You diagnose whether an SRE agent's "
    "output is correct and sufficient. Answer OK when it is good enough; answer RETRY only "
    "when a concrete, fixable problem exists, and say what to fix."
)

_GOALS = {
    "promql": "collect metric evidence that confirms or rules out the planned hypotheses",
    "node": "collect node-level evidence (logs, service state) explaining the alarms",
}


def make_reflection_node(
    step: str, deps: GraphDeps
) -> Callable[[IncidentState], Awaitable[dict]]:
    """Build the reflection node for one step of the graph."""

    async def reflect(state: IncidentState) -> dict:
        await emit(
            deps.events,
            type="agent.started",
            status="running",
            incident_id=state.get("incident_id", ""),
            cluster=state.get("cluster", ""),
            agent="reflection",
            step=f"reflect_{step}",
            title=f"Reflecting on {step} output",
            detail="Self-correction checkpoint reviewing the evidence just collected.",
        )
        config = deps.app_config.reflection
        retries = dict(state.get("retries") or {})
        used = retries.get(step, 0)

        if not config.enabled:
            return {"reflections": [{"step": step, "verdict": "OK", "feedback": "disabled"}]}

        evidence_key = "promql_evidence" if step == "promql" else "node_evidence"
        evidence = state.get(evidence_key) or []

        # An unreachable backend is an infrastructure fault, not a bad query. Re-prompting cannot
        # fix TLS or a refused connection, and asking costs an LLM call plus a full round of
        # re-execution per retry — the exact loop that produced 18 identical TLS errors in one
        # report during lab testing.
        if evidence and all(item.get("unreachable") for item in evidence):
            detail = evidence[0].get("result", "")
            logger.error("skipping %s reflection: backend unreachable (%s)", step, detail)
            await emit(
                deps.events,
                type="reflection.skipped",
                status="warn",
                incident_id=state.get("incident_id", ""),
                cluster=state.get("cluster", ""),
                agent="reflection",
                step=f"reflect_{step}",
                title=f"Reflection skipped — {step} backend unreachable",
                detail=detail,
            )
            return {
                "reflections": [
                    {"step": step, "verdict": "OK", "feedback": "backend unreachable"}
                ],
                "caveats": [
                    (
                        f"The {step} step could not reach its backend, so no evidence was "
                        f"collected. This is an infrastructure fault, not a diagnosis: {detail}"
                    )
                ],
            }

        if used >= config.max_retries:
            note = f"Accepted {step} evidence after {used} retries without a clean verdict."
            logger.info("reflection budget exhausted for step=%s", step)
            await emit(
                deps.events,
                type="reflection.exhausted",
                status="warn",
                incident_id=state.get("incident_id", ""),
                cluster=state.get("cluster", ""),
                agent="reflection",
                step=f"reflect_{step}",
                title=f"Retry budget exhausted for {step}",
                detail=note,
            )
            return {
                "reflections": [{"step": step, "verdict": "OK", "feedback": "budget exhausted"}],
                "caveats": [note],
            }

        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{incident_header(state)}\n\n"
                    f"Goal of this step: {_GOALS.get(step, step)}\n"
                    f"Evidence collected:\n{evidence_digest(evidence)}\n\n"
                    'Reply with JSON only: {"verdict": "OK" or "RETRY", "feedback": "..."}'
                ),
            },
        ]

        try:
            reflection = await deps.gateway.complete_json("reflection", messages, Reflection)
        except LLMError as exc:
            # A reflector that cannot be reached must not block the incident.
            logger.warning("reflection unavailable for step=%s: %s", step, exc)
            await emit(
                deps.events,
                type="reflection.offline",
                status="warn",
                incident_id=state.get("incident_id", ""),
                cluster=state.get("cluster", ""),
                agent="reflection",
                step=f"reflect_{step}",
                title=f"Reflector offline — {step} accepted",
                detail=str(exc),
            )
            return {
                "reflections": [{"step": step, "verdict": "OK", "feedback": "reflector offline"}],
                "caveats": [f"Reflection on the {step} step was skipped ({exc})."],
            }

        verdict = reflection.verdict
        if verdict == "RETRY":
            retries[step] = used + 1
            logger.info(
                "reflection asked for retry of step=%s (%d/%d): %s",
                step, retries[step], config.max_retries, reflection.feedback,
            )
            await emit(
                deps.events,
                type="reflection.retry",
                status="retry",
                incident_id=state.get("incident_id", ""),
                cluster=state.get("cluster", ""),
                agent="reflection",
                step=f"reflect_{step}",
                title=f"Reflection demands a retry of {step}",
                detail=reflection.feedback,
                data={"step": step, "retry": retries[step], "max": config.max_retries},
            )
        else:
            await emit(
                deps.events,
                type="reflection.approved",
                status="ok",
                incident_id=state.get("incident_id", ""),
                cluster=state.get("cluster", ""),
                agent="reflection",
                step=f"reflect_{step}",
                title=f"{step} output accepted",
                detail=reflection.feedback or "Reflector approved the evidence.",
                data={"step": step},
            )
        return {
            "reflections": [
                {"step": step, "verdict": verdict, "feedback": reflection.feedback}
            ],
            "retries": retries,
        }

    return reflect


def reflection_route(step: str, deps: GraphDeps) -> Callable[[IncidentState], str]:
    """Conditional edge: 'retry' back into the step, or 'ok' to move on."""

    def route(state: IncidentState) -> str:
        # The reflect node stops emitting RETRY once the budget is spent, so the verdict alone
        # is authoritative here — the loop cannot outlive max_retries.
        reflections = state.get("reflections") or []
        latest = next((r for r in reversed(reflections) if r.get("step") == step), None)
        return "retry" if latest and latest.get("verdict") == "RETRY" else "ok"

    return route
