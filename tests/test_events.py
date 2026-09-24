"""The event layer: emit/persist/replay and the SSE snapshot contract.

These tests run against the same fakeless pieces the app uses, so they also prove the UI's data
source behaves sensibly without needing langgraph/litellm.
"""

from __future__ import annotations

from tsagent.events import Event, EventBus, EventCollector, emit


async def test_emit_roundtrip_and_replay(repo):
    bus = EventBus()
    collector = EventCollector(bus, repo=repo)
    await collector.start()

    await emit(
        collector,
        type="incident.opened",
        status="ok",
        incident_id="inc-x",
        cluster="lab",
        title="Incident opened",
        detail="2 alarms",
    )
    await emit(
        collector,
        type="query.executed",
        status="ok",
        incident_id="inc-x",
        cluster="lab",
        title="Query answered",
    )

    recent = collector.recent(limit=10)
    assert [e.type for e in recent] == ["incident.opened", "query.executed"]
    assert recent[0].sequence == 1
    assert recent[1].sequence == 2

    # after= should skip older events
    assert [e.type for e in collector.recent(after=1)] == ["query.executed"]

    # persistence: a fresh collector started against the same repo replays history
    bus2 = EventBus()
    collector2 = EventCollector(bus2, repo=repo)
    await collector2.start()
    assert len(collector2.recent(limit=10)) == 2
    bus2.close()
    bus.close()


async def test_emit_without_collector_is_a_noop():
    # Graph nodes call emit unconditionally; a missing collector must never raise.
    await emit(None, type="system", status="info", title="hello")


async def test_collector_ring_buffer_bounds_growth():
    bus = EventBus()
    collector = EventCollector(bus, repo=None, maxlen=3)
    for i in range(10):
        await emit(collector, type="system", status="info", title=f"e{i}")
    assert len(collector.recent(limit=100)) == 3
    assert collector.recent(limit=100)[-1].title == "e9"
    bus.close()


async def test_event_is_serializable():
    ev = Event(type="query.executed", status="ok", title="x", data={"promql": "up"})
    dump = ev.summary_dict()
    assert dump["type"] == "query.executed"
    assert dump["data"]["promql"] == "up"


async def test_collector_persists_sequence(repo):
    bus = EventBus()
    collector = EventCollector(bus, repo=repo)
    await collector.start()
    await emit(collector, type="system", status="info", title="a")
    await emit(collector, type="system", status="info", title="b")

    stored = await repo.list_events()
    assert len(stored) == 2
    assert [e.sequence for e in stored] == [1, 2]
    bus.close()
