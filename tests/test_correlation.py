"""Correlation engine: grouping, dedupe, promotion timing and persistence."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tsagent.correlation import CorrelationEngine
from tsagent.models import Alarm, utcnow


def make_alarm(name="Down", node="overcloud-compute-0", fp=None, status="firing", **labels):
    return Alarm(
        fingerprint=fp or f"fp-{name}-{node}",
        status=status,
        name=name,
        node=node,
        severity=labels.pop("severity", "critical"),
        labels={"alertname": name, "instance": f"{node}:9100", "site": "lab", **labels},
    )


@pytest.fixture
def engine(app_config, inventory):
    return CorrelationEngine(config=app_config.correlation, inventory=inventory)


async def test_alarms_on_one_node_become_one_incident(engine):
    now = utcnow()
    await engine.add_alarm(make_alarm("NodeDown"), now=now)
    await engine.add_alarm(make_alarm("HighLoad"), now=now + timedelta(seconds=10))

    incidents = await engine.sweep(now=now + timedelta(minutes=10))

    assert len(incidents) == 1
    assert len(incidents[0].alarm_ids) == 2
    assert incidents[0].group_key == "lab-cluster-1|node=overcloud-compute-0"
    assert incidents[0].cluster == "lab-cluster-1"


async def test_different_nodes_stay_separate(engine):
    now = utcnow()
    await engine.add_alarm(make_alarm(node="overcloud-compute-0"), now=now)
    await engine.add_alarm(make_alarm(node="overcloud-compute-1"), now=now)

    incidents = await engine.sweep(now=now + timedelta(minutes=10))

    assert len(incidents) == 2


async def test_repeat_of_same_fingerprint_is_deduped(engine):
    now = utcnow()
    for i in range(5):
        await engine.add_alarm(make_alarm(fp="same-fp"), now=now + timedelta(seconds=i))

    incidents = await engine.sweep(now=now + timedelta(minutes=10))

    assert len(incidents) == 1
    assert len(incidents[0].alarm_ids) == 1


async def test_group_stays_buffered_inside_the_window(engine):
    now = utcnow()
    await engine.add_alarm(make_alarm(), now=now)

    assert await engine.sweep(now=now + timedelta(seconds=60)) == []
    assert len(engine.groups) == 1


async def test_self_resolved_group_raises_no_incident(engine):
    now = utcnow()
    await engine.add_alarm(make_alarm(fp="flap"), now=now)
    await engine.add_alarm(make_alarm(fp="flap", status="resolved"), now=now + timedelta(seconds=30))

    incidents = await engine.sweep(now=now + timedelta(minutes=10))

    assert incidents == []


async def test_continuously_alarming_node_still_promotes(engine):
    """The age cap is the whole point: a noisy node must not defer its own investigation."""
    now = utcnow()
    # A fresh alarm every four minutes keeps the quiet window from ever elapsing.
    for i in range(6):
        await engine.add_alarm(
            make_alarm(name=f"Alarm{i}", fp=f"fp{i}"), now=now + timedelta(minutes=4 * i)
        )
        promoted = await engine.sweep(now=now + timedelta(minutes=4 * i))
        if promoted:
            break

    assert promoted, "group exceeded max_group_age_seconds but was never promoted"
    assert promoted[0].group_key.endswith("node=overcloud-compute-0")


async def test_incident_is_persisted_and_alarms_are_stamped(app_config, inventory, repo):
    engine = CorrelationEngine(config=app_config.correlation, inventory=inventory, repo=repo)
    now = utcnow()
    alarm = make_alarm()
    await repo.save_alarm(alarm)
    await engine.add_alarm(alarm, now=now)

    incidents = await engine.sweep(now=now + timedelta(minutes=10))

    stored = await repo.get_incident(incidents[0].id)
    assert stored is not None
    reloaded = await repo.get_alarm(alarm.id)
    assert reloaded.incident_id == incidents[0].id
    assert await repo.list_alarms(incident_id=incidents[0].id)


async def test_on_incident_callback_fires(app_config, inventory):
    seen = []

    async def handler(incident):
        seen.append(incident)

    engine = CorrelationEngine(
        config=app_config.correlation, inventory=inventory, on_incident=handler
    )
    now = utcnow()
    await engine.add_alarm(make_alarm(), now=now)
    await engine.sweep(now=now + timedelta(minutes=10))

    assert len(seen) == 1


async def test_summary_reports_worst_severity_and_a_readable_scope(engine):
    now = utcnow()
    await engine.add_alarm(make_alarm("HighLoad", fp="a", severity="warning"), now=now)
    await engine.add_alarm(make_alarm("NodeDown", fp="b", severity="critical"), now=now)

    incidents = await engine.sweep(now=now + timedelta(minutes=10))

    summary = incidents[0].summary
    assert "[critical]" in summary
    assert "on node overcloud-compute-0" in summary
    assert "lab-cluster-1|" not in summary, "the cluster-prefixed key should not leak into prose"


async def test_nodeless_alarms_do_not_produce_a_redundant_scope(app_config, inventory):
    """Ingest backfills site from the cluster; the key must not become site=<cluster>."""
    engine = CorrelationEngine(config=app_config.correlation, inventory=inventory)
    now = utcnow()
    alarm = Alarm(
        fingerprint="fp-x", status="firing", name="hugepages", severity="major",
        node="", site="lab-cluster-1", labels={"alertname": "hugepages"},
    )
    await engine.add_alarm(alarm, now=now)

    incidents = await engine.sweep(now=now + timedelta(minutes=20))

    assert incidents[0].group_key == "lab-cluster-1|cluster"
    assert "on cluster lab-cluster-1" in incidents[0].summary


async def test_pending_summary_exposes_buffered_groups(engine):
    await engine.add_alarm(make_alarm())
    summary = engine.pending_summary()
    assert len(summary) == 1 and summary[0]["firing"] == 1
