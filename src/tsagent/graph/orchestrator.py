"""Runs incidents through the investigation graph.

Investigations are slow — minutes, on CPU inference — so the webhook never waits for one. The
correlation engine hands an incident here and the run proceeds as a tracked background task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from tsagent.graph.builder import build_graph
from tsagent.graph.deps import GraphDeps
from tsagent.models import Incident, IncidentStatus

logger = logging.getLogger(__name__)


class Orchestrator:
    """Owns the compiled graph, its checkpointer and the in-flight investigation tasks."""

    def __init__(
        self,
        deps: GraphDeps,
        checkpoint_path: Path | str | None = None,
        use_checkpointer: bool = True,
    ):
        self.deps = deps
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.use_checkpointer = use_checkpointer
        self._graph = None
        self._checkpointer_cm = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        checkpointer = None
        if self.use_checkpointer and self.checkpoint_path:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(str(self.checkpoint_path))
            checkpointer = await self._checkpointer_cm.__aenter__()
        self._graph = build_graph(self.deps, checkpointer=checkpointer)
        logger.info("orchestrator ready (checkpointing=%s)", bool(checkpointer))

    async def stop(self, grace_seconds: float = 5.0) -> None:
        # Give running investigations a moment to land their report before pulling the rug;
        # anything still going is cancelled and picked up again by resume_unfinished() on the
        # next start, using its LangGraph checkpoint.
        if self._tasks and grace_seconds:
            await self.drain(timeout=grace_seconds)
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._checkpointer_cm is not None:
            with contextlib.suppress(Exception):
                await self._checkpointer_cm.__aexit__(None, None, None)
            self._checkpointer_cm = None
        self._graph = None

    # -- running ------------------------------------------------------------

    async def build_initial_state(self, incident: Incident) -> dict[str, Any]:
        alarms: list[dict] = []
        if self.deps.repo:
            for alarm_id in incident.alarm_ids:
                alarm = await self.deps.repo.get_alarm(alarm_id)
                if alarm:
                    alarms.append(alarm.model_dump(mode="json"))
        return {
            "incident_id": incident.id,
            "cluster": incident.cluster,
            "incident": incident.model_dump(mode="json"),
            "alarms": alarms,
            "promql_evidence": [],
            "node_evidence": [],
            "reflections": [],
            "caveats": [],
            "retries": {},
        }

    async def investigate(self, incident: Incident) -> dict[str, Any]:
        """Run one incident to completion and return the final state."""
        if self._graph is None:
            raise RuntimeError("Orchestrator.start() was not called")

        if self.deps.repo:
            incident.status = IncidentStatus.INVESTIGATING
            await self.deps.repo.save_incident(incident)

        state = await self.build_initial_state(incident)
        config = {"configurable": {"thread_id": incident.id}}
        logger.info("investigating incident %s (%d alarms)", incident.id, len(state["alarms"]))
        await _emit(
            self,
            type="incident.started",
            status="running",
            incident_id=incident.id,
            cluster=incident.cluster,
            step="investigate",
            title="Investigation started",
            detail=f"{incident.summary or incident.group_key} — {len(state['alarms'])} alarm(s).",
        )
        try:
            return await self._graph.ainvoke(state, config=config)
        except Exception:
            logger.exception("investigation of %s failed", incident.id)
            await _emit(
                self,
                type="incident.failed",
                status="error",
                incident_id=incident.id,
                cluster=incident.cluster,
                step="investigate",
                title="Investigation failed",
                detail="The investigation run raised an exception; see pod logs.",
            )
            if self.deps.repo:
                incident.status = IncidentStatus.FAILED
                await self.deps.repo.save_incident(incident)
            raise

    def submit(self, incident: Incident) -> asyncio.Task:
        """Start an investigation in the background; the caller does not wait."""
        task = asyncio.create_task(self.investigate(incident))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def handle_incident(self, incident: Incident) -> None:
        """Callback shape the correlation engine expects."""
        self.submit(incident)

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for in-flight investigations to finish."""
        pending = set(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def resume_unfinished(self) -> list[str]:
        """Re-submit incidents left mid-investigation by a restart or a cancelled shutdown.

        Without this the checkpointer buys nothing: an incident that was interrupted would sit in
        the store as ``investigating`` forever and no operator would ever get its report. The
        graph picks up from its last checkpoint, keyed by incident id.
        """
        if not self.deps.repo:
            return []
        stale = await self.deps.repo.list_incidents_by_status(
            [IncidentStatus.OPEN.value, IncidentStatus.INVESTIGATING.value]
        )
        for incident in stale:
            logger.info("resuming unfinished incident %s (%s)", incident.id, incident.status)
            self.submit(incident)
        return [incident.id for incident in stale]

    @property
    def in_flight(self) -> int:
        return len(self._tasks)


async def _emit(
    orchestrator: Orchestrator,
    *,
    type: str,
    status: str = "info",
    incident_id: str = "",
    cluster: str = "",
    node: str = "",
    step: str = "",
    title: str = "",
    detail: str = "",
) -> None:
    """Forward an orchestrator-level event through the shared collector, if present."""
    from tsagent.events import emit

    await emit(
        getattr(orchestrator.deps, "events", None),
        type=type,
        status=status,
        incident_id=incident_id,
        cluster=cluster,
        node=node,
        agent="orchestrator",
        step=step,
        title=title,
        detail=detail,
    )
