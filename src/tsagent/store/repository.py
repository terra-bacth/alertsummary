"""SQLite case store (aiosqlite). Repository for alarms, incidents, evidence, reports."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from tsagent.models import Alarm, Evidence, Incident, Report

try:  # events live in their own module, imported lazily to avoid a cycle
    from tsagent.events import Event as _Event
except Exception:  # noqa: BLE001 - events.py always exists; guard is belt and braces
    _Event = None  # type: ignore[assignment]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    incident_id TEXT,
    received_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarms_fingerprint ON alarms(fingerprint);
CREATE INDEX IF NOT EXISTS idx_alarms_incident ON alarms(incident_id);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence(incident_id);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_incident ON reports(incident_id);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    incident_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_incident ON events(incident_id);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
"""


class Repository:
    """Async SQLite repository. One instance per app; call connect()/close()."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA)
        await db.commit()
        self._db = db

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Repository not connected"
        return self._db

    # -- alarms -------------------------------------------------------------

    async def save_alarm(self, alarm: Alarm) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO alarms (id, fingerprint, status, incident_id, received_at, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                alarm.id,
                alarm.fingerprint,
                alarm.status,
                alarm.incident_id,
                alarm.received_at.isoformat(),
                alarm.model_dump_json(),
            ),
        )
        await self.db.commit()

    async def get_alarm(self, alarm_id: str) -> Alarm | None:
        cur = await self.db.execute("SELECT data FROM alarms WHERE id = ?", (alarm_id,))
        row = await cur.fetchone()
        return Alarm.model_validate_json(row["data"]) if row else None

    async def list_alarms(self, incident_id: str | None = None, limit: int = 200) -> list[Alarm]:
        if incident_id:
            cur = await self.db.execute(
                "SELECT data FROM alarms WHERE incident_id = ? ORDER BY received_at DESC LIMIT ?",
                (incident_id, limit),
            )
        else:
            cur = await self.db.execute(
                "SELECT data FROM alarms ORDER BY received_at DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [Alarm.model_validate_json(r["data"]) for r in rows]

    # -- incidents ----------------------------------------------------------

    async def save_incident(self, incident: Incident) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO incidents (id, status, created_at, data) VALUES (?, ?, ?, ?)",
            (
                incident.id,
                incident.status.value,
                incident.created_at.isoformat(),
                incident.model_dump_json(),
            ),
        )
        await self.db.commit()

    async def get_incident(self, incident_id: str) -> Incident | None:
        cur = await self.db.execute("SELECT data FROM incidents WHERE id = ?", (incident_id,))
        row = await cur.fetchone()
        return Incident.model_validate_json(row["data"]) if row else None

    async def list_incidents(self, limit: int = 100) -> list[Incident]:
        cur = await self.db.execute(
            "SELECT data FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [Incident.model_validate_json(r["data"]) for r in rows]

    async def list_incidents_by_status(self, statuses: list[str], limit: int = 100) -> list[Incident]:
        # Only the placeholder count is interpolated; the status values stay bound parameters.
        placeholders = ",".join("?" for _ in statuses)
        cur = await self.db.execute(
            f"SELECT data FROM incidents WHERE status IN ({placeholders})"
            " ORDER BY created_at ASC LIMIT ?",
            (*statuses, limit),
        )
        rows = await cur.fetchall()
        return [Incident.model_validate_json(r["data"]) for r in rows]

    # -- evidence -----------------------------------------------------------

    async def save_evidence(self, ev: Evidence) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO evidence (id, incident_id, created_at, data) VALUES (?, ?, ?, ?)",
            (ev.id, ev.incident_id, ev.created_at.isoformat(), ev.model_dump_json()),
        )
        await self.db.commit()

    async def list_evidence(self, incident_id: str) -> list[Evidence]:
        cur = await self.db.execute(
            "SELECT data FROM evidence WHERE incident_id = ? ORDER BY created_at ASC",
            (incident_id,),
        )
        rows = await cur.fetchall()
        return [Evidence.model_validate_json(r["data"]) for r in rows]

    # -- reports ------------------------------------------------------------

    async def save_report(self, report: Report) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO reports (id, incident_id, created_at, data) VALUES (?, ?, ?, ?)",
            (report.id, report.incident_id, report.created_at.isoformat(), report.model_dump_json()),
        )
        await self.db.commit()

    async def get_report_for_incident(self, incident_id: str) -> Report | None:
        cur = await self.db.execute(
            "SELECT data FROM reports WHERE incident_id = ? ORDER BY created_at DESC LIMIT 1",
            (incident_id,),
        )
        row = await cur.fetchone()
        return Report.model_validate_json(row["data"]) if row else None

    # -- events -------------------------------------------------------------

    async def save_event(self, event, sequence: int) -> None:
        """Persist one tsagent event. Imported lazily so the schema stays loader-agnostic."""
        if (_Event is not None and isinstance(event, _Event)) or hasattr(
            event, "model_dump_json"
        ):
            data = event.model_dump_json()
        else:  # pragma: no cover - defensive; callers pass Event instances
            data = str(event)
        await self.db.execute(
            "INSERT OR REPLACE INTO events (id, seq, incident_id, ts, data) VALUES (?, ?, ?, ?, ?)",
            (event.id, sequence, event.incident_id, event.ts.isoformat(), data),
        )
        await self.db.commit()

    async def list_events(self, incident_id: str | None = None, limit: int = 4000) -> list:
        """Recent events, optionally scoped to one incident.

        Uses a tolerant loader that falls back to a dict so the repository does not hard-depend
        on the events module resolving at runtime.
        """
        if incident_id:
            cur = await self.db.execute(
                "SELECT data FROM events WHERE incident_id = ? ORDER BY seq DESC LIMIT ?",
                (incident_id, limit),
            )
        else:
            cur = await self.db.execute(
                "SELECT data FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        events: list[object] = []
        for row in reversed(rows):  # chronological order, oldest first
            data = row["data"]
            try:
                if _Event is not None:
                    events.append(_Event.model_validate_json(data))
                    continue
            except Exception:  # noqa: BLE001, S110 - fall back to plain dict below
                pass
            import json as _json

            try:
                events.append(_json.loads(data))
            except ValueError:
                continue
        return events
