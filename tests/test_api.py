"""API: webhook ingest, correlation endpoints, and the full alarm-to-report journey."""

from __future__ import annotations

from tests.conftest import SAMPLE_WEBHOOK


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_readyz(client):
    r = await client.get("/readyz")
    assert r.status_code == 200


async def test_webhook_ingest(client):
    r = await client.post("/alerts", json=SAMPLE_WEBHOOK)
    assert r.status_code == 202
    assert r.json()["accepted"] == 2

    alarms = (await client.get("/alarms")).json()
    assert {a["name"] for a in alarms} == {"NodeDown", "HighLoad"}
    assert all(a["site"] in ("lab", "lab-cluster-1") for a in alarms)


async def test_webhook_rejects_bad_payload(client):
    r = await client.post("/alerts", json={"alerts": "not-a-list"})
    assert r.status_code == 422


async def test_incident_404(client):
    r = await client.get("/incidents/inc-missing")
    assert r.status_code == 404


async def test_report_404(client):
    r = await client.get("/incidents/inc-missing/report")
    assert r.status_code == 404


async def test_report_format_is_validated(client):
    r = await client.get("/incidents/inc-x/report?format=exe")
    assert r.status_code == 422


async def test_ingested_alarms_are_buffered_not_immediately_promoted(client):
    """AlertManager gets a fast 202; correlation deliberately waits for the window."""
    await client.post("/alerts", json=SAMPLE_WEBHOOK)

    pending = (await client.get("/correlation/pending")).json()["groups"]
    assert len(pending) == 1
    assert pending[0]["firing"] == 2
    assert (await client.get("/incidents")).json() == []


async def test_alarms_to_report_end_to_end(client):
    """Webhook in, correlated incident out, RCA report retrievable in both formats."""
    await client.post("/alerts", json=SAMPLE_WEBHOOK)

    flushed = await client.post("/correlation/flush")
    assert flushed.status_code == 202
    incident_ids = flushed.json()["promoted"]
    assert len(incident_ids) == 1

    from tsagent.main import app

    await app.state.orchestrator.drain(timeout=10)

    incident_id = incident_ids[0]
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["incident"]["cluster"] == "lab-cluster-1"
    assert len(detail["alarms"]) == 2

    report = (await client.get(f"/incidents/{incident_id}/report")).json()
    assert report["incident_id"] == incident_id
    assert report["recommendations"]

    markdown = await client.get(f"/incidents/{incident_id}/report?format=md")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text.startswith("# Incident")
    assert "NodeDown" in markdown.text


async def test_flush_without_a_correlator_is_a_clear_error(client):
    from tsagent.main import app

    correlator = app.state.correlator
    app.state.correlator = None
    try:
        r = await client.post("/correlation/flush")
        assert r.status_code == 503
    finally:
        app.state.correlator = correlator
