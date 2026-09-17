"""Prompt-context helpers.

Everything an agent sees about an incident is built here, and it is built small. On CPU-hosted
inference the cost of a step scales with what you feed it, so evidence is summarized on the way
in rather than dumped raw.
"""

from __future__ import annotations

import re
from typing import Any


def alarm_digest(alarms: list[dict[str, Any]], limit: int = 12) -> str:
    """One line per alarm: enough to reason about, far short of the raw payload."""
    if not alarms:
        return "(no alarms)"
    lines = []
    for alarm in alarms[:limit]:
        summary = (alarm.get("annotations") or {}).get("summary", "")
        starts = alarm.get("starts_at") or ""
        lines.append(
            f"- {alarm.get('name')} [{alarm.get('severity')}] node={alarm.get('node') or '?'} "
            f"started={starts} {summary}".rstrip()
        )
        # The rule's own expression names the exact metric that fired. Without it a model will
        # guess at a metric namespace, which is wrong on any stack that is not node_exporter.
        expr = alarm.get("rule_expr")
        if expr:
            lines.append(f"    rule: {expr[:300]}")
    if len(alarms) > limit:
        lines.append(f"- ... and {len(alarms) - limit} more alarms")
    return "\n".join(lines)


def metric_namespaces(alarms: list[dict[str, Any]]) -> list[str]:
    """Metric name prefixes seen in the firing rules, e.g. ['collectd_'] or ['node_'].

    Lets the PromQL agent be told which exporter it is actually querying instead of defaulting
    to node_exporter names that return nothing on a collectd/STF stack.
    """
    seen: list[str] = []
    for alarm in alarms:
        for match in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)_[a-zA-Z0-9_]+\b", alarm.get("rule_expr") or ""):
            prefix = f"{match.group(1).split('_')[0]}_"
            if prefix not in seen and not prefix.startswith(("sum_", "rate_", "avg_", "by_")):
                seen.append(prefix)
    return seen[:4]


def evidence_digest(
    evidence: list[dict[str, Any]],
    limit: int = 12,
    max_chars: int = 4000,
    result_chars: int = 400,
) -> str:
    """Render collected evidence within a hard total character budget.

    The budget is what protects small models: podman ps / pcs status output can
    be thousands of chars, and a single oversized result repeated across retries
    is what pushes the report agent past a 4096-token window. Each result is
    first capped at ``result_chars``, then the whole digest is clamped at
    ``max_chars`` — truncating the last entry if needed — so callers can rely on
    the return value never exceeding ``max_chars`` characters.
    """
    if not evidence:
        return "(none collected)"

    lines: list[str] = []
    used = 0
    shown = 0

    for item in evidence[:limit]:
        status = "ok" if item.get("ok", True) else "failed"
        request = str(item.get("request") or "")
        result = str(item.get("result") or "")

        if len(result) > result_chars:
            result = (
                result[:result_chars]
                + f" ... [{len(result) - result_chars} chars omitted]"
            )

        entry = f"- [{status}] {request}\n    -> {result}"

        remaining = max_chars - used
        if remaining <= 0:
            break

        if len(entry) > remaining:
            # Reserve room for the marker so the total stays <= max_chars.
            marker = " ... [digest truncated]"
            keep = remaining - len(marker)
            if keep <= 0:
                break  # no room for a truncated entry + marker
            entry = entry[:keep].rstrip() + marker

        lines.append(entry)
        used += len(entry)
        shown += 1

        if used >= max_chars:
            break

    omitted = len(evidence) - shown
    if omitted > 0 and used < max_chars:
        suffix = f"- ... and {omitted} more results"
        if used + len(suffix) <= max_chars:
            lines.append(suffix)

    return "\n".join(lines)


def incident_header(state: dict[str, Any]) -> str:
    incident = state.get("incident") or {}
    return (
        f"Incident {state.get('incident_id', '?')} on RHOSP cluster "
        f"{state.get('cluster') or 'unknown'} (grouped by {incident.get('group_key', '?')}).\n"
        f"Alarms:\n{alarm_digest(state.get('alarms') or [])}"
    )


def affected_nodes(state: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for alarm in state.get("alarms") or []:
        node = alarm.get("node")
        if node and node not in seen:
            seen.append(node)
    return seen
