"""Shared state for the investigation graph.

Agents communicate through this typed state rather than free-form chat: for ops automation the
value is in being able to say exactly what each step saw and produced, and to resume a half-done
incident from a checkpoint. Everything here stays JSON-serializable for that reason.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _extend(left: list, right: list) -> list:
    """Reducer so nodes can append evidence without clobbering earlier entries."""
    return (left or []) + (right or [])


class IncidentState(TypedDict, total=False):
    incident_id: str
    cluster: str
    incident: dict[str, Any]
    alarms: list[dict[str, Any]]

    plan: dict[str, Any]
    promql_evidence: Annotated[list[dict[str, Any]], _extend]
    node_evidence: Annotated[list[dict[str, Any]], _extend]
    reflections: Annotated[list[dict[str, Any]], _extend]
    caveats: Annotated[list[str], _extend]

    retries: dict[str, int]
    decision: str
    decision_reason: str
    target_nodes: list[str]
    report: dict[str, Any]
    error: str
