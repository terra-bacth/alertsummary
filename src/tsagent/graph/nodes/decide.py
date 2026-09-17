"""Decision node: is the metric evidence enough, or do we need to log into a node?

Deterministic rules run first. Node access is the expensive, higher-risk path, so the cases where
metrics are obviously insufficient (or obviously sufficient) are settled without an LLM call.
"""

from __future__ import annotations

import logging

from tsagent.graph.context import affected_nodes, evidence_digest, incident_header
from tsagent.graph.deps import GraphDeps
from tsagent.graph.schemas import Decision
from tsagent.graph.state import IncidentState
from tsagent.llm import LLMError

logger = logging.getLogger(__name__)

SYSTEM = (
    "You decide whether metric evidence already explains an incident, or whether logging into "
    "the node is required. Node access is expensive, so prefer metric-only resolution when "
    "the root cause is genuinely deducible from metrics alone."
    "\n\n"
    "CRITICAL: If ANY query returns a result confirming the alarm symptom (e.g. "
    "health_status=0 meaning the service IS down), that is the SYMPTOM, not the cause. "
    "You MUST choose need_node_access because the root cause is unknown. The alarm already "
    "tells you something is down; node investigation finds out WHY."
    "\n\n"
    "Rules:"
    "\n- need_node_access: when metrics confirm the problem but don't explain why. "
    "Examples: health_status=0, the affected node itself returns no data, "
    "or all queries return empty for that specific node."
    "\n- sufficient: when metrics clearly show a root cause (disk 100% full, "
    "memory exhaustion, error rate spiking), or when all metrics show a healthy "
    "system and the alarm may be transient."
    "\n- sufficient: when no director/SSH is configured."
)


async def decide_node(state: IncidentState, deps: GraphDeps) -> dict:
    evidence = state.get("promql_evidence") or []
    nodes = affected_nodes(state)

    verdict = _rule_decision(state, evidence, nodes, deps)
    if verdict:
        decision, reason = verdict
        logger.info("decision (rule): %s — %s", decision, reason)
        return {"decision": decision, "decision_reason": reason, "target_nodes": nodes}

    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f"{incident_header(state)}\n\n"
                f"Hypotheses: {'; '.join((state.get('plan') or {}).get('hypotheses') or []) or 'none'}\n"
                f"Metric evidence:\n{evidence_digest(evidence)}\n\n"
                'Reply with JSON only: {"decision": "sufficient" or "need_node_access", '
                '"reason": "...", "target_nodes": ["..."]}'
            ),
        },
    ]

    try:
        decision = await deps.gateway.complete_json("decision", messages, Decision)
    except LLMError as exc:
        # Defaulting to "sufficient" keeps a degraded run read-only rather than sending an
        # unreviewed agent onto production nodes.
        logger.warning("decision LLM unavailable, reporting on metrics alone: %s", exc)
        return {
            "decision": "sufficient",
            "decision_reason": f"decision model unavailable ({exc})",
            "target_nodes": nodes,
            "caveats": ["Node investigation was skipped because the decision model was offline."],
        }

    targets = [n for n in decision.target_nodes if n in nodes] or nodes
    logger.info("decision (llm): %s — %s", decision.decision, decision.reason)
    return {
        "decision": decision.decision,
        "decision_reason": decision.reason,
        "target_nodes": targets,
    }


def _rule_decision(
    state: IncidentState, evidence: list[dict], nodes: list[str], deps: GraphDeps
) -> tuple[str, str] | None:
    """Settle the unambiguous cases without spending an LLM call."""
    if not nodes:
        return "sufficient", "no specific node is implicated, so there is nothing to log into"

    if deps.ssh_for(state.get("cluster") or "") is None:
        return "sufficient", "no director is configured for this cluster, so SSH is unavailable"

    if not evidence:
        return "need_node_access", "no metric evidence was collected at all"

    if all(e.get("unreachable") for e in evidence):
        return "need_node_access", "Prometheus is unreachable, so metrics are blind"

    if all(not e.get("ok", True) for e in evidence):
        return "need_node_access", "every metric query failed"

    # An unreachable node exporter is precisely the case metrics cannot explain on their own.
    for item in evidence:
        result = str(item.get("result", ""))
        if item.get("ok") and "empty result" in result and "up" in str(item.get("request", "")):
            return "need_node_access", "the node's exporter returns nothing, so metrics are blind"
    
    # A container health check returning 0 confirms the symptom (service down) but
    # does not explain why — escalation is needed for the root cause.
    for item in evidence:
        result = str(item.get("result", ""))
        if item.get("ok") and "health_status" in result and "= 0" in result:
            return "need_node_access",                 "container health check confirms the service is down (= 0) but does not explain why"

    return None
