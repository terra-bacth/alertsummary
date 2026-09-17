"""PromQL agent: generates queries from the plan, validates them, executes them.

Queries are generated in one batch rather than one call per check — on CPU inference the round
trips dominate, and the checks are independent enough that batching costs nothing in quality.
"""

from __future__ import annotations

import logging

from tsagent.graph.context import affected_nodes, incident_header, metric_namespaces
from tsagent.graph.deps import GraphDeps
from tsagent.graph.schemas import QueryBatch
from tsagent.graph.state import IncidentState
from tsagent.llm import LLMError
from tsagent.models import Evidence, EvidenceKind
from tsagent.tools.prometheus import inject_label_matchers

logger = logging.getLogger(__name__)

SYSTEM = (
    "You write PromQL for a RHOSP monitoring stack. Use only metric names you have been shown. "
    "CRITICAL: NEVER include [1m], [5m], [1h] or any [N] range selector. "
    "Any query containing [ ] (square brackets) will be REJECTED before execution. "
    "Use ONLY instant vector queries without range selectors. "
    "Examples:\n"
    "  sum by (host) (collectd_memory{type_instance='used'})\n"
    "  100 * sum by (host) (collectd_memory{type_instance='used'}) / sum by (host) (collectd_memory)\n"
    "  collectd_hugepages_vmpage_number{type_instance='free'}\n"
    "Return valid PromQL only, NO square brackets. Set range_query to false."
)


async def promql_node(state: IncidentState, deps: GraphDeps) -> dict:
    cluster = state.get("cluster") or ""
    client = deps.prometheus_for(cluster)
    if client is None:
        return {
            "caveats": [f"No Prometheus is configured for cluster {cluster!r}; metrics skipped."]
        }

    plan = state.get("plan") or {}
    checks = plan.get("checks") or []
    if not checks:
        return {"caveats": ["The plan contained no metric checks."]}

    matchers = deps.inventory.label_matchers_for(cluster)
    nodes = affected_nodes(state)
    goals = "\n".join(
        f"{i + 1}. {c.get('goal')} (hint: {c.get('metric_hint') or 'none'})"
        for i, c in enumerate(checks)
    )

    # Ground the model in the metric namespace that actually fired. On a collectd/STF stack,
    # node_exporter names silently return nothing, which looks identical to a healthy system.
    namespaces = metric_namespaces(state.get("alarms") or [])
    namespace_note = (
        f"This Prometheus exposes metrics named {', '.join(n + '*' for n in namespaces)} — "
        "use that namespace, not metrics from a different exporter.\n"
        if namespaces
        else ""
    )

    # Operator knowledge: worked queries from the library, and the metric names that exist here.
    knowledge = deps.knowledge_for(cluster)
    goal_texts = [str(c.get("goal", "")) for c in checks]
    alarm_names = [str(a.get("name", "")) for a in state.get("alarms") or []]
    context = knowledge.prompt_context(goal_texts + alarm_names)

    parts = [
        incident_header(state),
        "",
        f"Affected nodes: {', '.join(nodes) or 'unknown'}",
    ]
    if namespace_note:
        parts.append(namespace_note.rstrip())
    if context:
        parts.extend(["", context])
    parts.extend(
        [
            "",
            f"Write one PromQL query per goal:\n{goals}",
            "",
            (
                'Reply with JSON only: {"queries": [{"goal": "...", "promql": "...", '
                '"range_query": false}]}'
            ),
        ]
    )

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]

    retry_feedback = _latest_feedback(state, "promql")
    if retry_feedback:
        messages.append(
            {"role": "user", "content": f"Previous attempt was rejected: {retry_feedback}"}
        )

    try:
        batch = await deps.gateway.complete_json("promql", messages, QueryBatch)
    except LLMError as exc:
        logger.error("promql generation failed: %s", exc)
        return {"caveats": [f"PromQL generation failed ({exc}); no metric evidence collected."]}

    max_queries = deps.app_config.investigation.max_promql_queries
    evidence: list[dict] = []

    already_run = {e.get("request") for e in state.get("promql_evidence") or []}

    for query in batch.queries[:max_queries]:
        scoped = inject_label_matchers(query.promql, matchers)

        # Pre-validate: reject range vectors in instant queries only.
        # Queries with range_query=true MAY contain valid range vectors e.g. rate(...[5m])
        has_range = "[" in scoped and "]" in scoped
        if has_range and not query.range_query:
            logger.warning(
                "rejecting instant query with range vector: %s", scoped,
            )
            evidence.append(
                {
                    "kind": "promql",
                    "goal": query.goal,
                    "request": scoped,
                    "result": "REJECTED: instant query contains a range vector [N] — "
                              "remove the range selector or set range_query=true",
                    "ok": False,
                    "unreachable": False,
                }
            )
            continue

        # Reject invented metrics before execution. Prometheus answers a query for a metric that
        # does not exist with an empty result — identical to a healthy system — so without this
        # check a hallucinated name reads as evidence of health.
        unknown = knowledge.catalogue.unknown_metrics(scoped)
        if unknown:
            suggestions = knowledge.catalogue.suggest(unknown[0])
            detail = f"metric(s) not present on this Prometheus: {', '.join(unknown)}"
            if suggestions:
                detail += f". Did you mean: {', '.join(suggestions)}?"
            logger.warning("rejected query %r: %s", scoped, detail)
            evidence.append(
                {
                    "kind": "promql",
                    "goal": query.goal,
                    "request": scoped,
                    "result": f"NOT EXECUTED: {detail}",
                    "ok": False,
                    "unreachable": False,
                }
            )
            continue

        if scoped in already_run:
            # A retry that regenerates an identical query has nothing new to tell us.
            logger.info("skipping duplicate query: %s", scoped)
            continue
        already_run.add(scoped)
        # Prometheus returns its parse error in the 400 body, so a malformed query comes back as
        # ok=False with the reason attached — that is what the reflection step reads.
        if query.range_query:
            result = await client.query_range(scoped)
        else:
            result = await client.query(scoped)

        record = {
            "kind": "promql",
            "goal": query.goal,
            "request": scoped,
            "result": result.summarize(),
            "ok": result.ok,
            "unreachable": result.unreachable,
        }
        evidence.append(record)

        if deps.repo:
            await deps.repo.save_evidence(
                Evidence(
                    incident_id=state["incident_id"],
                    kind=EvidenceKind.PROMQL,
                    source=cluster,
                    request=scoped,
                    result=record["result"],
                    ok=result.ok,
                )
            )

        if result.unreachable:
            # Prometheus itself is not answering. The remaining queries would fail identically,
            # so stop rather than producing a wall of the same error. Persisted first, so the
            # reason the investigation stopped is in the audit trail.
            logger.error("prometheus unreachable for cluster %s: %s", cluster, result.error)
            break

    failures = sum(1 for e in evidence if not e["ok"])
    logger.info("promql step collected %d results (%d failed)", len(evidence), failures)
    return {"promql_evidence": evidence}


def _latest_feedback(state: IncidentState, step: str) -> str:
    for reflection in reversed(state.get("reflections") or []):
        if reflection.get("step") == step:
            return reflection.get("feedback", "")
    return ""
