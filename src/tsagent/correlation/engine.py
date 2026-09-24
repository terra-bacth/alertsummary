"""Correlation engine: buffers incoming alarms and promotes coherent groups to incidents.

Grouping is deterministic (rules, not an LLM): alarms land in a group keyed by cluster + node,
falling back to cluster + site and then the cluster itself. A group is promoted once it has been
quiet for ``window_seconds`` or has been open for ``max_group_age_seconds`` — the age cap exists
because a node that alarms continuously would otherwise keep refreshing its own quiet window and
never get investigated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from tsagent.config import ClusterInventory, CorrelationConfig
from tsagent.models import Alarm, Incident, IncidentStatus, utcnow
from tsagent.store import Repository

logger = logging.getLogger(__name__)

IncidentHandler = Callable[[Incident], Awaitable[None]]


async def _emit(
    engine: CorrelationEngine,
    *,
    type: str,
    status: str = "info",
    incident_id: str = "",
    cluster: str = "",
    node: str = "",
    agent: str = "correlator",
    step: str = "correlate",
    title: str = "",
    detail: str = "",
    data: dict | None = None,
) -> None:
    """Forward a correlation-level event through the shared collector, if present."""
    from tsagent.events import emit

    await emit(
        engine.events,
        type=type,
        status=status,
        incident_id=incident_id,
        cluster=cluster,
        node=node,
        agent=agent,
        step=step,
        title=title,
        detail=detail,
        data=data,
    )


class AlarmGroup:
    """Alarms buffered under one grouping key, deduped by fingerprint."""

    def __init__(self, key: str, cluster: str, now: datetime, scope: str = ""):
        self.key = key
        self.cluster = cluster
        self.scope = scope or key  # human-readable version of the key, for prose
        self.first_seen = now
        self.last_seen = now
        self.alarms: dict[str, Alarm] = {}  # fingerprint -> most recent alarm

    def add(self, alarm: Alarm, now: datetime) -> None:
        """Add or replace an alarm. A repeat of the same fingerprint updates in place."""
        self.alarms[alarm.fingerprint] = alarm
        self.last_seen = now

    @property
    def firing(self) -> list[Alarm]:
        return [a for a in self.alarms.values() if a.status == "firing"]

    def is_ready(self, now: datetime, cfg: CorrelationConfig) -> bool:
        quiet_for = (now - self.last_seen).total_seconds()
        open_for = (now - self.first_seen).total_seconds()
        return quiet_for >= cfg.window_seconds or open_for >= cfg.max_group_age_seconds

    def summarize(self) -> str:
        names = sorted({a.name for a in self.firing})
        worst = _worst_severity([a.severity for a in self.firing])
        return f"{len(self.firing)} firing alarm(s) [{worst}] on {self.scope}: {', '.join(names)}"


_SEVERITY_ORDER = ["critical", "major", "error", "warning", "minor", "info", "unknown"]


def _worst_severity(severities: list[str]) -> str:
    for level in _SEVERITY_ORDER:
        if level in severities:
            return level
    return "unknown"


class CorrelationEngine:
    """Buffers alarms and emits incidents.

    Not thread-safe by design — it is driven from the FastAPI event loop and guarded by an
    asyncio lock so the background sweeper and webhook handler cannot interleave.
    """

    def __init__(
        self,
        config: CorrelationConfig,
        inventory: ClusterInventory,
        repo: Repository | None = None,
        on_incident: IncidentHandler | None = None,
        events: object | None = None,
    ):
        self.config = config
        self.inventory = inventory
        self.repo = repo
        self.on_incident = on_incident
        self.events = events
        self.groups: dict[str, AlarmGroup] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None

    # -- grouping -----------------------------------------------------------

    def group_key_for(self, alarm: Alarm) -> tuple[str, str, str]:
        """Return (group_key, cluster, scope). Node is the most specific useful grouping.

        The site fallback is skipped when the site label just restates the cluster — the ingest
        layer backfills `site` from the resolved cluster, which would otherwise produce keys like
        `lab-cluster-1|site=lab-cluster-1`.
        """
        cluster = self.inventory.resolve_cluster(alarm.labels) or alarm.site or "unknown"
        if alarm.node:
            return f"{cluster}|node={alarm.node}", cluster, f"node {alarm.node}"
        if alarm.site and alarm.site != cluster:
            return f"{cluster}|site={alarm.site}", cluster, f"site {alarm.site}"
        return f"{cluster}|cluster", cluster, f"cluster {cluster}"

    async def add_alarm(self, alarm: Alarm, now: datetime | None = None) -> None:
        now = now or utcnow()
        key, cluster, scope = self.group_key_for(alarm)
        async with self._lock:
            group = self.groups.get(key)
            if group is None:
                group = AlarmGroup(key, cluster, now, scope=scope)
                self.groups[key] = group
            group.add(alarm, now)
        logger.info(
            "alarm buffered: group=%s name=%s status=%s severity=%s (group holds %d)",
            key, alarm.name, alarm.status, alarm.severity, len(group.alarms),
        )
        await _emit(
            self,
            type="alarm.buffered",
            status="info",
            cluster=cluster,
            node=alarm.node,
            agent="correlator",
            step="correlate",
            title=f"{alarm.name} buffered",
            detail=f"{alarm.status} [{alarm.severity}] on {scope} — group holds {len(group.alarms)}.",
            data={"alarm": alarm.model_dump(mode="json")},
        )

    # -- promotion ----------------------------------------------------------

    async def sweep(self, now: datetime | None = None) -> list[Incident]:
        """Promote every ready group. Returns the incidents created."""
        now = now or utcnow()
        async with self._lock:
            ready = [g for g in self.groups.values() if g.is_ready(now, self.config)]
            for group in ready:
                del self.groups[group.key]

        incidents: list[Incident] = []
        for group in ready:
            incident = await self._promote(group)
            if incident:
                incidents.append(incident)
        return incidents

    async def _promote(self, group: AlarmGroup) -> Incident | None:
        firing = group.firing
        if not firing:
            # Everything in the group resolved itself inside the window — nothing to investigate.
            logger.info("group %s self-resolved, no incident raised", group.key)
            await _emit(
                self,
                type="correlate.resolved",
                status="ok",
                cluster=group.cluster,
                agent="correlator",
                step="correlate",
                title="Group self-resolved",
                detail=f"{group.scope} went quiet inside the window — no incident raised.",
            )
            return None

        incident = Incident(
            cluster=group.cluster,
            group_key=group.key,
            alarm_ids=[a.id for a in firing],
            summary=group.summarize(),
            status=IncidentStatus.OPEN,
        )

        if self.repo:
            await self.repo.save_incident(incident)
            for alarm in firing:
                alarm.incident_id = incident.id
                await self.repo.save_alarm(alarm)

        logger.info(
            "incident raised: id=%s cluster=%s group=%s alarms=%d",
            incident.id, incident.cluster, incident.group_key, len(firing),
        )
        await _emit(
            self,
            type="incident.opened",
            status="ok",
            incident_id=incident.id,
            cluster=incident.cluster,
            agent="correlator",
            step="correlate",
            title="Incident opened",
            detail=incident.summary,
            data={"incident": incident.model_dump(mode="json")},
        )
        if self.on_incident:
            await self.on_incident(incident)
        return incident

    # -- background sweeper -------------------------------------------------

    async def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweeper is None:
            return
        self._sweeper.cancel()
        try:
            await self._sweeper
        except asyncio.CancelledError:
            pass
        self._sweeper = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.sweep_interval_seconds)
            try:
                await self.sweep()
            except Exception:  # a bad group must not kill the sweeper
                logger.exception("correlation sweep failed")

    # -- introspection ------------------------------------------------------

    def pending_summary(self) -> list[dict]:
        return [
            {
                "group_key": g.key,
                "cluster": g.cluster,
                "alarms": len(g.alarms),
                "firing": len(g.firing),
                "age_seconds": int((utcnow() - g.first_seen).total_seconds()),
                "quiet_seconds": int((utcnow() - g.last_seen).total_seconds()),
                "promotes_at": (
                    g.last_seen + timedelta(seconds=self.config.window_seconds)
                ).isoformat(),
            }
            for g in self.groups.values()
        ]
