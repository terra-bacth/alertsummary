"""Tests for the SQLite repository."""

from tsagent.models import Alarm, Evidence, EvidenceKind, Incident, IncidentStatus, Report


async def test_alarm_roundtrip(repo):
    alarm = Alarm(fingerprint="fp1", status="firing", name="NodeDown", node="compute-0")
    await repo.save_alarm(alarm)
    loaded = await repo.get_alarm(alarm.id)
    assert loaded == alarm


async def test_incident_and_evidence(repo):
    inc = Incident(cluster="lab-cluster-1", group_key="node=compute-0", alarm_ids=["a1"])
    await repo.save_incident(inc)

    ev = Evidence(
        incident_id=inc.id,
        kind=EvidenceKind.PROMQL,
        source="prom-lab",
        request='up{instance="compute-0:9100"}',
        result="0 (down)",
    )
    await repo.save_evidence(ev)

    loaded = await repo.get_incident(inc.id)
    assert loaded is not None and loaded.status == IncidentStatus.OPEN
    evidence = await repo.list_evidence(inc.id)
    assert len(evidence) == 1 and evidence[0].kind == EvidenceKind.PROMQL


async def test_incident_update_is_upsert(repo):
    inc = Incident()
    await repo.save_incident(inc)
    inc.status = IncidentStatus.REPORTED
    await repo.save_incident(inc)
    incidents = await repo.list_incidents()
    assert len(incidents) == 1 and incidents[0].status == IncidentStatus.REPORTED


async def test_report_roundtrip(repo):
    rpt = Report(incident_id="inc-x", root_cause="disk full", confidence=0.9,
                 recommendations=["clean /var/log"], markdown="# RCA")
    await repo.save_report(rpt)
    loaded = await repo.get_report_for_incident("inc-x")
    assert loaded is not None and loaded.root_cause == "disk full"
