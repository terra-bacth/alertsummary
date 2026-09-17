"""Investigation graph: routing, reflection loops, escalation and degraded paths."""

from __future__ import annotations

import pytest

from tests.conftest import FakeGateway, FakePrometheus, FakeSSH
from tsagent.graph import Orchestrator
from tsagent.models import Alarm, Incident, IncidentStatus
from tsagent.tools.prometheus import QueryResult

PLAN = {
    "hypotheses": ["The node exporter died", "The host is gone"],
    "checks": [
        {"goal": "Is the exporter up?", "metric_hint": "up"},
        {"goal": "Filesystem headroom", "metric_hint": "node_filesystem_avail_bytes"},
    ],
}

QUERIES = {
    "queries": [
        {"goal": "Is the exporter up?", "promql": 'up{job="node-exporter"}', "range_query": False},
        {"goal": "Filesystem headroom", "promql": "node_filesystem_avail_bytes", "range_query": False},
    ]
}

RCA = {
    "root_cause": "The /var filesystem filled and the exporter stopped",
    "confidence": 0.8,
    "recommendations": ["Rotate logs in /var/log/containers", "Add a disk-usage alert at 80%"],
    "caveats": [],
    "timeline": ["Disk filled", "Exporter stopped", "NodeDown fired"],
}

OK = {"verdict": "OK", "feedback": ""}


@pytest.fixture
async def incident(repo):
    """An incident with two alarms on one compute node, persisted."""
    alarms = [
        Alarm(
            fingerprint="fp1", status="firing", name="NodeDown", severity="critical",
            node="overcloud-compute-0", site="lab",
            labels={"alertname": "NodeDown", "instance": "overcloud-compute-0:9100", "site": "lab"},
            annotations={"summary": "exporter down"},
        ),
        Alarm(
            fingerprint="fp2", status="firing", name="HighLoad", severity="warning",
            node="overcloud-compute-0", site="lab",
            labels={"alertname": "HighLoad", "instance": "overcloud-compute-0:9100", "site": "lab"},
        ),
    ]
    for alarm in alarms:
        await repo.save_alarm(alarm)
    inc = Incident(
        cluster="lab-cluster-1",
        group_key="lab-cluster-1|node=overcloud-compute-0",
        alarm_ids=[a.id for a in alarms],
        summary="2 firing alarm(s) [critical] on overcloud-compute-0",
    )
    await repo.save_incident(inc)
    return inc


async def run(deps, incident):
    orchestrator = Orchestrator(deps, checkpoint_path=None, use_checkpointer=False)
    await orchestrator.start()
    try:
        return await orchestrator.investigate(incident)
    finally:
        await orchestrator.stop()


# -- happy path -------------------------------------------------------------


async def test_metrics_alone_produce_a_report(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": QUERIES,
            "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "metrics explain it"},
            "report": RCA,
        }
    )
    prometheus = FakePrometheus(
        {
            "up": QueryResult(
                expr="up", ok=True, result_type="vector",
                series=[{"metric": {"__name__": "up"}, "value": [1, "0"]}],
            ),
            "node_filesystem": QueryResult(
                expr="fs", ok=True, result_type="vector",
                series=[{"metric": {"__name__": "node_filesystem_avail_bytes"}, "value": [1, "1024"]}],
            ),
        }
    )
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    assert state["decision"] == "sufficient"
    assert len(state["promql_evidence"]) == 2
    assert state["node_evidence"] == []
    assert "filesystem filled" in state["report"]["root_cause"]


async def test_queries_are_scoped_to_the_incident_cluster(make_deps, repo, incident):
    """A shared Prometheus must not answer with another cluster's metrics."""
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES, "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "ok"}, "report": RCA,
        }
    )
    prometheus = FakePrometheus()
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    await run(deps, incident)

    assert prometheus.queries
    assert all('site="lab"' in q for q in prometheus.queries)


async def test_report_and_incident_status_are_persisted(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES, "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "ok"}, "report": RCA,
        }
    )
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo)

    await run(deps, incident)

    report = await repo.get_report_for_incident(incident.id)
    assert report is not None
    assert report.recommendations
    assert "# Incident" in report.markdown
    stored = await repo.get_incident(incident.id)
    assert stored.status == IncidentStatus.REPORTED
    assert await repo.list_evidence(incident.id)


# -- escalation to node access ---------------------------------------------


async def test_escalates_to_node_investigation_when_metrics_are_insufficient(
    make_deps, repo, incident
):
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": QUERIES,
            "reflection": [OK, OK],
            "decision": {
                "decision": "need_node_access",
                "reason": "metrics are ambiguous",
                "target_nodes": ["overcloud-compute-0"],
            },
            "node_agent": [
                {"node": "overcloud-compute-0", "command": "df -h", "rationale": "check disk"},
                {"done": True},
            ],
            "report": RCA,
        }
    )
    ssh = FakeSSH({"df -h": "/dev/vda1  50G  50G  0  100% /var"})
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=ssh, repo=repo)

    state = await run(deps, incident)

    assert state["decision"] == "need_node_access"
    assert ssh.executed == [("overcloud-compute-0", "df -h")]
    assert "100%" in state["node_evidence"][0]["result"]
    assert state["report"]["markdown"].count("df -h") >= 1


async def test_a_refused_command_is_recorded_and_does_not_stop_the_run(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": QUERIES,
            "reflection": [OK, OK],
            "decision": {"decision": "need_node_access", "reason": "need logs"},
            "node_agent": [
                {"node": "overcloud-compute-0", "command": "rm -rf /var/log", "rationale": "clean"},
                {"node": "overcloud-compute-0", "command": "df -h", "rationale": "check disk"},
                {"done": True},
            ],
            "report": RCA,
        }
    )
    ssh = FakeSSH({"df -h": "plenty of space"})
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=ssh, repo=repo)

    state = await run(deps, incident)

    assert ("overcloud-compute-0", "rm -rf /var/log") not in ssh.executed
    refusals = [e for e in state["node_evidence"] if e["result"].startswith("REFUSED")]
    assert len(refusals) == 1
    assert state["report"]["root_cause"]


async def test_node_command_budget_is_enforced(make_deps, repo, incident, app_config):
    """max_node_commands is 3 in the test config; a model that never stops must be stopped."""
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": QUERIES,
            "reflection": [OK, OK],
            "decision": {"decision": "need_node_access", "reason": "need logs"},
            "node_agent": {"node": "overcloud-compute-0", "command": "df -h", "rationale": "again"},
            "report": RCA,
        }
    )
    ssh = FakeSSH()
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=ssh, repo=repo)

    state = await run(deps, incident)

    assert len(ssh.executed) == app_config.investigation.max_node_commands
    assert any("budget" in c for c in state["caveats"])


async def test_no_director_configured_keeps_the_run_metrics_only(make_deps, repo, incident):
    gateway = FakeGateway({"planner": PLAN, "promql": QUERIES, "reflection": OK, "report": RCA})
    # ssh=None means no director for this cluster; the rule decision must not ask for node access.
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=None, repo=repo)

    state = await run(deps, incident)

    assert state["decision"] == "sufficient"
    assert "no director is configured" in state["decision_reason"]


# -- reflection -------------------------------------------------------------


async def test_reflection_retry_reruns_the_step(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": [QUERIES, QUERIES],
            "reflection": [{"verdict": "RETRY", "feedback": "query the right job label"}, OK],
            "decision": {"decision": "sufficient", "reason": "ok"},
            "report": RCA,
        }
    )
    prometheus = FakePrometheus()
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    assert gateway.agents_called().count("promql") == 2
    assert state["retries"]["promql"] == 1
    # The second attempt is told why the first was rejected.
    second_promql = [m for a, m in gateway.calls if a == "promql"][1]
    assert any("query the right job label" in m["content"] for m in second_promql)


async def test_reflection_loop_cannot_outlive_its_budget(make_deps, repo, incident, app_config):
    always_retry = {"verdict": "RETRY", "feedback": "not good enough"}
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": QUERIES,
            "reflection": always_retry,
            "decision": {"decision": "sufficient", "reason": "ok"},
            "report": RCA,
        }
    )
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    max_retries = app_config.reflection.max_retries
    assert gateway.agents_called().count("promql") == max_retries + 1
    assert any("retries" in c for c in state["caveats"])


async def test_reflection_can_be_disabled(make_deps, repo, incident, app_config):
    app_config.reflection.enabled = False
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES,
            "decision": {"decision": "sufficient", "reason": "ok"}, "report": RCA,
        }
    )
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    # No "reflection" response is scripted, so a call would have raised.
    assert state["reflections"][0]["feedback"] == "disabled"
    assert state["report"]["root_cause"]


# -- degraded paths ---------------------------------------------------------


async def test_planner_outage_falls_back_to_a_generic_sweep(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "promql": QUERIES, "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "ok"}, "report": RCA,
        }
    )
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    assert state["plan"]["checks"]
    assert any("Planner LLM unavailable" in c for c in state["caveats"])
    assert state["report"]["root_cause"]


async def test_total_llm_outage_still_yields_a_report_with_the_evidence(make_deps, repo, incident):
    """A model outage must degrade to a human-readable report, not to nothing at all."""
    deps = make_deps(gateway=FakeGateway({}), prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    report = await repo.get_report_for_incident(incident.id)
    assert report is not None
    assert "Undetermined" in report.root_cause
    assert len(state["caveats"]) >= 2


async def test_missing_prometheus_is_reported_as_a_caveat(make_deps, repo, incident):
    gateway = FakeGateway(
        {"planner": PLAN, "reflection": OK, "report": RCA}
    )
    deps = make_deps(gateway=gateway, prometheus=None, ssh=None, repo=repo)

    state = await run(deps, incident)

    assert any("No Prometheus is configured" in c for c in state["caveats"])
    assert state["promql_evidence"] == []


async def test_decision_defaults_to_no_ssh_when_the_model_is_offline(make_deps, repo, incident):
    gateway = FakeGateway({"planner": PLAN, "promql": QUERIES, "reflection": OK, "report": RCA})
    prometheus = FakePrometheus(
        {
            "up": QueryResult(
                expr="up", ok=True, result_type="vector",
                series=[{"metric": {"__name__": "up"}, "value": [1, "1"]}],
            )
        }
    )
    ssh = FakeSSH()
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=ssh, repo=repo)

    state = await run(deps, incident)

    assert state["decision"] == "sufficient"
    assert ssh.executed == []
    assert any("decision model was offline" in c for c in state["caveats"])


async def test_failed_investigation_marks_the_incident_failed(make_deps, repo, incident):
    class ExplodingGateway(FakeGateway):
        async def complete_json(self, *args, **kwargs):
            raise RuntimeError("boom")

    deps = make_deps(gateway=ExplodingGateway(), prometheus=FakePrometheus(), repo=repo)
    orchestrator = Orchestrator(deps, checkpoint_path=None, use_checkpointer=False)
    await orchestrator.start()
    try:
        with pytest.raises(RuntimeError):
            await orchestrator.investigate(incident)
    finally:
        await orchestrator.stop()

    stored = await repo.get_incident(incident.id)
    assert stored.status == IncidentStatus.FAILED


# -- restart recovery -------------------------------------------------------


async def test_unfinished_incidents_are_resumed_on_start(make_deps, repo, incident):
    """A pod restart mid-investigation must not strand the incident forever."""
    incident.status = IncidentStatus.INVESTIGATING
    await repo.save_incident(incident)

    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES, "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "ok"}, "report": RCA,
        }
    )
    deps = make_deps(gateway=gateway, prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo)
    orchestrator = Orchestrator(deps, checkpoint_path=None, use_checkpointer=False)
    await orchestrator.start()
    try:
        resumed = await orchestrator.resume_unfinished()
        await orchestrator.drain(timeout=10)
    finally:
        await orchestrator.stop()

    assert resumed == [incident.id]
    stored = await repo.get_incident(incident.id)
    assert stored.status == IncidentStatus.REPORTED
    assert await repo.get_report_for_incident(incident.id) is not None


async def test_reported_incidents_are_not_resumed(make_deps, repo, incident):
    incident.status = IncidentStatus.REPORTED
    await repo.save_incident(incident)

    deps = make_deps(gateway=FakeGateway({}), repo=repo)
    orchestrator = Orchestrator(deps, checkpoint_path=None, use_checkpointer=False)
    await orchestrator.start()
    try:
        assert await orchestrator.resume_unfinished() == []
    finally:
        await orchestrator.stop()


# -- unreachable backend ----------------------------------------------------


def _unreachable(expr="up"):
    return QueryResult(
        expr=expr, ok=False, unreachable=True,
        error="[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain",
    )


async def test_tls_failure_does_not_burn_the_reflection_budget(make_deps, repo, incident):
    """Re-prompting cannot fix TLS. This produced 18 identical errors in one lab report."""
    gateway = FakeGateway(
        {
            "planner": PLAN,
            "promql": QUERIES,
            "reflection": OK,  # only the node step should reach this
            "node_agent": [{"done": True}],
            "report": {
                "root_cause": "Undetermined", "confidence": 0.1,
                "recommendations": ["Fix the Prometheus certificate"], "timeline": [],
            },
        }
    )
    prometheus = FakePrometheus({"up": _unreachable(), "node_filesystem": _unreachable()})
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    # The promql step ran once and was never re-executed on the model's say-so.
    assert gateway.agents_called().count("promql") == 1
    promql_reflection = next(r for r in state["reflections"] if r["step"] == "promql")
    assert promql_reflection == {
        "step": "promql", "verdict": "OK", "feedback": "backend unreachable"
    }
    assert any("infrastructure fault" in c for c in state["caveats"])


async def test_unreachable_metrics_still_escalate_to_the_node(make_deps, repo, incident):
    """Blind metrics are exactly when logging into the box is worth it."""
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES, "reflection": OK,
            "node_agent": [
                {"node": "overcloud-compute-0", "command": "free -m", "rationale": "check memory"},
                {"done": True},
            ],
            "report": {"root_cause": "Memory exhausted", "confidence": 0.5,
                       "recommendations": ["Rebalance guests"], "timeline": []},
        }
    )
    prometheus = FakePrometheus({"up": _unreachable(), "node_filesystem": _unreachable()})
    ssh = FakeSSH({"free -m": "Mem: 128000 127500 500"})
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=ssh, repo=repo)

    state = await run(deps, incident)

    assert state["decision"] == "need_node_access"
    assert "unreachable" in state["decision_reason"]
    assert ssh.executed == [("overcloud-compute-0", "free -m")]


async def test_remaining_queries_are_skipped_once_prometheus_is_unreachable(
    make_deps, repo, incident
):
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES,
            "decision": {"decision": "sufficient", "reason": "x"},
            "report": {"root_cause": "Undetermined", "confidence": 0.1,
                       "recommendations": ["fix tls"], "timeline": []},
        }
    )
    prometheus = FakePrometheus({"up": _unreachable(), "node_filesystem": _unreachable()})
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    # The plan had two checks; the second is pointless once the backend is proven unreachable.
    assert len(prometheus.queries) == 1
    assert len(state["promql_evidence"]) == 1


async def test_confidence_is_capped_when_nothing_was_verified(make_deps, repo, incident):
    """A model with no evidence still answers confidently; the number must not follow it."""
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES,
            "decision": {"decision": "sufficient", "reason": "x"},
            "report": {
                "root_cause": "Certificate verification failed for the monitoring system",
                "confidence": 1.0,
                "recommendations": ["Fix the certificate"],
                "timeline": [],
            },
        }
    )
    prometheus = FakePrometheus({"up": _unreachable(), "node_filesystem": _unreachable()})
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    state = await run(deps, incident)

    report = await repo.get_report_for_incident(incident.id)
    assert report.confidence <= 0.2
    assert any("not been verified" in c for c in report.caveats)
    assert "0%" in state["report"]["markdown"] or "20%" in state["report"]["markdown"]


async def test_confidence_is_untouched_when_evidence_was_collected(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": PLAN, "promql": QUERIES, "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "x"}, "report": RCA,
        }
    )
    prometheus = FakePrometheus(
        {
            "up": QueryResult(expr="up", ok=True, result_type="vector",
                              series=[{"metric": {"__name__": "up"}, "value": [1, "0"]}]),
        }
    )
    deps = make_deps(gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo)

    await run(deps, incident)

    report = await repo.get_report_for_incident(incident.id)
    assert report.confidence == 0.8


# -- metric catalogue grounding ---------------------------------------------


def _knowledge():
    from tests.conftest import CONFIG_DIR
    from tsagent.knowledge import Knowledge, MetricCatalogue, PromQLLibrary

    return Knowledge(
        catalogue=MetricCatalogue(
            ["collectd_hugepages_vmpage_number", "collectd_memory", "collectd_cpu_percent", "up"]
        ),
        library=PromQLLibrary.load(CONFIG_DIR / "promql_library.yaml"),
    )


async def test_an_invented_metric_never_reaches_prometheus(make_deps, repo, incident):
    """The lab ran 18 queries for metrics that do not exist and read the empties as healthy."""
    gateway = FakeGateway(
        {
            "planner": {
                "hypotheses": ["hugepages exhausted"],
                "checks": [{"goal": "Verify current hugepage usage", "metric_hint": ""}],
            },
            "promql": {
                "queries": [
                    {"goal": "Verify current hugepage usage",
                     "promql": "sum(node_hugepages_used)", "range_query": False}
                ]
            },
            "reflection": [OK, OK],
            "decision": {"decision": "sufficient", "reason": "x"},
            "report": {"root_cause": "Undetermined", "confidence": 0.1,
                       "recommendations": ["check the catalogue"], "timeline": []},
        }
    )
    prometheus = FakePrometheus()
    deps = make_deps(
        gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo, knowledge=_knowledge()
    )

    state = await run(deps, incident)

    assert prometheus.queries == [], "a non-existent metric must not be executed"
    evidence = state["promql_evidence"][0]
    assert evidence["ok"] is False
    assert "not present on this Prometheus" in evidence["result"]
    # And the report tells the operator what to use instead.
    assert "collectd_hugepages_vmpage_number" in evidence["result"]


async def test_a_real_metric_is_executed_normally(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": {"hypotheses": ["h"],
                        "checks": [{"goal": "hugepage usage", "metric_hint": ""}]},
            "promql": {
                "queries": [
                    {"goal": "hugepage usage",
                     "promql": 'sum by (host) (collectd_hugepages_vmpage_number{type_instance="used"})',
                     "range_query": False}
                ]
            },
            "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "x"},
            "report": RCA,
        }
    )
    prometheus = FakePrometheus()
    deps = make_deps(
        gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo, knowledge=_knowledge()
    )

    await run(deps, incident)

    assert len(prometheus.queries) == 1
    assert "collectd_hugepages_vmpage_number" in prometheus.queries[0]


async def test_library_examples_are_shown_to_the_promql_agent(make_deps, repo, incident):
    gateway = FakeGateway(
        {
            "planner": {"hypotheses": ["h"],
                        "checks": [{"goal": "Verify current hugepage usage", "metric_hint": ""}]},
            "promql": {"queries": []},
            "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "x"},
            "report": RCA,
        }
    )
    deps = make_deps(
        gateway=gateway, prometheus=FakePrometheus(), ssh=FakeSSH(), repo=repo,
        knowledge=_knowledge(),
    )

    await run(deps, incident)

    prompt = next(m for a, m in gateway.calls if a == "promql")[-1]["content"]
    assert "Known-good queries" in prompt
    assert "collectd_hugepages_vmpage_number" in prompt
    assert "use ONLY names from this list" in prompt


async def test_duplicate_queries_across_retries_are_skipped(make_deps, repo, incident):
    """The lab report ran the same six queries three times, byte for byte."""
    same = {
        "queries": [
            {"goal": "hugepage usage", "promql": "collectd_memory", "range_query": False}
        ]
    }
    gateway = FakeGateway(
        {
            "planner": {"hypotheses": ["h"],
                        "checks": [{"goal": "hugepage usage", "metric_hint": ""}]},
            "promql": [same, dict(same)],
            "reflection": [{"verdict": "RETRY", "feedback": "try harder"}, OK],
            "decision": {"decision": "sufficient", "reason": "x"},
            "report": RCA,
        }
    )
    prometheus = FakePrometheus()
    deps = make_deps(
        gateway=gateway, prometheus=prometheus, ssh=FakeSSH(), repo=repo, knowledge=_knowledge()
    )

    state = await run(deps, incident)

    assert gateway.agents_called().count("promql") == 2, "the retry did happen"
    assert len(prometheus.queries) == 1, "but the identical query was not re-executed"
    assert len(state["promql_evidence"]) == 1
