"""Event bus and collector: the observability backbone for the UI.

The investigation graph runs for minutes in the background, and until now the only thing an
operator could watch was the pod logs. This module turns the pipeline into a typed event stream:

    ingest -> correlate (incident open) -> plan -> promql -> reflect -> decide
           -> node (SSH) -> reflect -> report

Every graph node, the correlation engine and the orchestrator publish small, JSON-serializable
events. The UI subscribes over Server-Sent Events (``GET /api/stream``) and replays history from
``GET /api/events``. Events are also persisted to the case store (a new ``events`` table) so the
timeline survives a pod restart, matching the existing SQLite model.

Design constraints kept in mind:

* The collector is optional everywhere. Graph nodes receive it via ``GraphDeps.events`` which
  defaults to ``None``, so every existing test and every degraded path is unchanged when it is
  absent.
* Publishing is fire-and-forget from the point of view of the investigation: an event that
  cannot be persisted (e.g. repo closed) is dropped with a logged warning, never an exception
  that could fail an incident.
* No heavy imports. This module must stay importable with only the core extras installed
  (fastapi/pydantic are enough), so the REST layer and unit tests do not need langgraph/litellm.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from tsagent.models import new_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """One observable thing that happened inside a tsagent investigation."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    ts: datetime = Field(default_factory=_now)
    # Monotonic sequence assigned by the collector at publish time; used for ``after=`` resume
    # and for persistence. Not set by callers.
    sequence: int = 0
    # Dotted event type, e.g. "query.executed", "report.ready". The UI maps this to a pipeline
    # stage and to a presentation style (chip color, icon).
    type: str = "system"
    # One of the status buckets the UI paints: info | running | ok | warn | error | retry |
    # refused | unreachable | debug. Kept coarse on purpose — a small vocab is easier to style
    # and to reason about than a free-form one.
    status: str = "info"
    # Scope.
    incident_id: str = ""
    cluster: str = ""
    node: str = ""
    # Agent that produced it (planner/promql/reflect/decision/node_agent/report/system).
    agent: str = ""
    # Pipeline stage (plan/promql/reflect_promql/decide/node/reflect_node/report/ingest/correlate).
    step: str = ""
    # Human-readable summary and a longer line, mirroring what used to go to the logs only.
    title: str = ""
    detail: str = ""
    # Structured payload for the detail views: a query, a command, a verdict, a report summary.
    data: dict[str, Any] = Field(default_factory=dict)

    def summary_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Event bus (pub/sub for Server-Sent Events)
# ---------------------------------------------------------------------------


class EventBus:
    """In-process fan-out of events to any number of async subscriber queues.

    Single-replica by design, which matches how tsagent is deployed. Subscribers pull from an
    ``asyncio.Queue``; when a slow client falls behind, the oldest events are dropped from its
    queue rather than unbounded buffering.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._closed = False

    def subscribe(self, maxsize: int = 512) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    async def publish(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("event subscriber queue is full; dropping event")

    def close(self) -> None:
        self._closed = True
        self._queues.clear()


# ---------------------------------------------------------------------------
# Collector: ring buffer + optional persistence
# ---------------------------------------------------------------------------


class EventCollector:
    """Assigns sequence numbers, persists events (when a repo is available) and publishes them.

    ``seq`` is a monotonic counter so a reconnecting client can ask ``GET /api/events?after=42``
    and resume exactly where it left off instead of replaying the whole history.
    """

    def __init__(
        self,
        bus: EventBus,
        repo: Any | None = None,
        maxlen: int = 4000,
    ) -> None:
        self.bus = bus
        self.repo = repo
        self.maxlen = maxlen
        self._ring: list[Event] = []
        self._seq = 0
        self._hydrated = False

    async def start(self) -> None:
        """Replay history from the store when it is available, so restarts do not blank the UI."""
        if not self.repo or self._hydrated:
            return
        self._hydrated = True
        try:
            if hasattr(self.repo, "list_events"):
                stored = await self.repo.list_events(limit=self.maxlen)
                self._ring = list(stored)
                if self._ring:
                    self._seq = max(e.sequence for e in self._ring)
                    logger.info("event collector replayed %d stored events (last seq=%d)",
                                len(self._ring), self._seq)
        except Exception as exc:  # noqa: BLE001 - replay must never break startup
            logger.warning("could not replay stored events: %s", exc)

    async def emit(self, event: Event) -> Event:
        self._seq += 1
        event.sequence = self._seq
        payload = {
            **event.summary_dict(),
            "seq": self._seq,
        }
        self._ring.append(event)
        if len(self._ring) > self.maxlen:
            self._ring = self._ring[-self.maxlen :]

        # Persist first (best-effort) so the replay endpoint and the live stream agree.
        if self.repo and hasattr(self.repo, "save_event"):
            try:
                await self.repo.save_event(event, sequence=self._seq)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to persist event %s: %s", event.id, exc)

        await self.bus.publish(payload)
        return event

    def recent(self, limit: int = 100, after: int | None = None) -> list[Event]:
        events = self._ring
        if after is not None:
            events = [e for e in events if e.sequence > after]
        return events[-limit:]

    @property
    def last_seq(self) -> int:
        return self._seq

    def __len__(self) -> int:
        return len(self._ring)


# ---------------------------------------------------------------------------
# Helper used by graph nodes and the API layer
# ---------------------------------------------------------------------------


async def emit(
    collector: EventCollector | None,
    *,
    type: str,
    status: str = "info",
    incident_id: str = "",
    cluster: str = "",
    node: str = "",
    agent: str = "",
    step: str = "",
    title: str = "",
    detail: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    """Publish an event through ``collector`` when one is available; a no-op otherwise.

    Graph nodes already stay fully functional without a collector, so they can call this
    unconditionally and tests (and deployments compiled without the UI) never notice.
    """
    if collector is None:
        return
    try:
        await collector.emit(
            Event(
                type=type,
                status=status,
                incident_id=incident_id,
                cluster=cluster,
                node=node,
                agent=agent,
                step=step,
                title=title,
                detail=detail,
                data=data or {},
            )
        )
    except Exception as exc:  # noqa: BLE001 - observability must never fail the investigation
        logger.warning("event emit failed (%s): %s", type, exc)
