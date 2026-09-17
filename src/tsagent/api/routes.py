"""API routes: AlertManager webhook ingest, incident/report retrieval, health probes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from tsagent.models import Alarm, AMWebhook

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    ready = getattr(request.app.state, "repo", None) is not None
    if not ready:
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


@router.post("/alerts", status_code=202)
async def receive_alerts(payload: AMWebhook, request: Request) -> dict:
    """AlertManager webhook receiver.

    Returns as soon as the alarms are persisted and buffered. Correlation happens on a timer and
    investigation takes minutes, so making AlertManager wait for either would only cause it to
    time out and redeliver.
    """
    repo = request.app.state.repo
    inventory = request.app.state.inventory
    correlator = getattr(request.app.state, "correlator", None)

    stored: list[str] = []
    for am_alert in payload.alerts:
        alarm = Alarm.from_am_alert(am_alert)
        cluster = inventory.resolve_cluster(alarm.labels)
        if cluster and not alarm.site:
            alarm.site = cluster
        await repo.save_alarm(alarm)
        stored.append(alarm.id)
        if correlator:
            await correlator.add_alarm(alarm)
        logger.info(
            "alarm received: name=%s status=%s node=%s cluster=%s fp=%s",
            alarm.name, alarm.status, alarm.node, cluster, alarm.fingerprint,
        )
    return {"accepted": len(stored), "alarm_ids": stored}


@router.get("/alarms")
async def list_alarms(request: Request, limit: int = 100) -> list:
    repo = request.app.state.repo
    return [a.model_dump(mode="json") for a in await repo.list_alarms(limit=limit)]


@router.get("/correlation/pending")
async def pending_groups(request: Request) -> dict:
    """What the correlation engine is currently holding, and when it will promote."""
    correlator = getattr(request.app.state, "correlator", None)
    if correlator is None:
        return {"groups": []}
    return {"groups": correlator.pending_summary()}


@router.post("/correlation/flush", status_code=202)
async def flush_correlation(request: Request) -> dict:
    """Promote every buffered group immediately. Useful for demos and drills."""
    correlator = getattr(request.app.state, "correlator", None)
    if correlator is None:
        raise HTTPException(status_code=503, detail="correlation engine not running")
    incidents = await correlator.sweep(now=_far_future())
    return {"promoted": [i.id for i in incidents]}


@router.get("/incidents")
async def list_incidents(request: Request, limit: int = 50) -> list:
    repo = request.app.state.repo
    return [i.model_dump(mode="json") for i in await repo.list_incidents(limit=limit)]


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request) -> dict:
    repo = request.app.state.repo
    incident = await repo.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="incident not found")
    evidence = await repo.list_evidence(incident_id)
    alarms = await repo.list_alarms(incident_id=incident_id)
    return {
        "incident": incident.model_dump(mode="json"),
        "alarms": [a.model_dump(mode="json") for a in alarms],
        "evidence": [e.model_dump(mode="json") for e in evidence],
    }


@router.get("/incidents/{incident_id}/report")
async def get_report(
    incident_id: str,
    request: Request,
    format: str = Query(default="json", pattern="^(json|md)$"),
):
    repo = request.app.state.repo
    report = await repo.get_report_for_incident(incident_id)
    if not report:
        raise HTTPException(status_code=404, detail="report not found")
    if format == "md":
        return PlainTextResponse(report.markdown, media_type="text/markdown")
    return report.model_dump(mode="json")


def _far_future():
    from datetime import timedelta

    from tsagent.models import utcnow

    return utcnow() + timedelta(days=365)
