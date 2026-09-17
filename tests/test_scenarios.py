"""Canned incident scenarios, end to end.

Scope note: the LLM is faked, so these do not measure whether a model reasons well — that is what
the Phase 7 prompt-iteration work against real models is for. What they do pin down is the part
the system is responsible for: that for a given alarm shape the agent asks Prometheus the right
kind of question, escalates to node access only when it should, keeps every command inside the
allow-list, and lands the diagnostic facts in the report an operator will read.
"""

from __future__ import annotations

import pytest

from tests.conftest import FakeGateway, FakePrometheus, FakeSSH
from tsagent.correlation import CorrelationEngine
from tsagent.graph import Orchestrator
from tsagent.models import Alarm, utcnow
from tsagent.tools.prometheus import QueryResult

OK = {"verdict": "OK", "feedback": ""}


def alarm(name, node="overcloud-compute-0", severity="critical", **extra_labels):
    return Alarm(
        fingerprint=f"fp-{name}-{node}",
        status="firing",
        name=name,
        severity=severity,
        node=node,
        site="lab",
        labels={"alertname": name, "instance": f"{node}:9100", "site": "lab", **extra_labels},
        annotations={"summary": f"{name} on {node}"},
    )


def vector(name, value, **labels):
    return QueryResult(
        expr=name,
        ok=True,
        result_type="vector",
        series=[{"metric": {"__name__": name, **labels}, "value": [1, str(value)]}],
    )


async def run_scenario(repo, app_config, inventory, allowlist, alarms, gateway, prometheus, ssh):
    """Drive the whole pipeline: alarms -> correlation -> investigation -> report."""
    from tsagent.graph import GraphDeps

    for a in alarms:
        await repo.save_alarm(a)

    deps = GraphDeps(
        gateway=gateway,
        inventory=inventory,
        app_config=app_config,
        allowlist=allowlist,
        repo=repo,
        prometheus_factory=lambda _c: prometheus,
        ssh_factory=lambda _c: ssh,
    )
    orchestrator = Orchestrator(deps, checkpoint_path=None, use_checkpointer=False)
    await orchestrator.start()

    engine = CorrelationEngine(
        config=app_config.correlation,
        inventory=inventory,
        repo=repo,
        on_incident=orchestrator.handle_incident,
    )
    now = utcnow()
    for a in alarms:
        await engine.add_alarm(a, now=now)
    incidents = await engine.sweep(now=now.replace(year=now.year + 1))

    try:
        await orchestrator.drain(timeout=10)
    finally:
        await orchestrator.stop()

    reports = []
    for incident in incidents:
        report = await repo.get_report_for_incident(incident.id)
        reports.append((incident, report))
    return reports


@pytest.fixture
def scenario(repo, app_config, inventory, allowlist):
    async def _run(alarms, gateway, prometheus=None, ssh=None):
        return await run_scenario(
            repo, app_config, inventory, allowlist,
            alarms, gateway, prometheus or FakePrometheus(), ssh,
        )

    return _run


# -- scenario 1: disk full --------------------------------------------------


async def test_disk_full_reaches_the_node_and_reports_the_full_filesystem(scenario):
    gateway = FakeGateway(
        {
            "planner": {
                "hypotheses": ["/var filled and stopped the service"],
                "checks": [{"goal": "Filesystem headroom", "metric_hint": "node_filesystem_avail_bytes"}],
            },
            "promql": {
                "queries": [
                    {
                        "goal": "Filesystem headroom",
                        "promql": 'node_filesystem_avail_bytes{mountpoint="/var"}',
                        "range_query": False,
                    }
                ]
            },
            "reflection": [OK, OK],
            "decision": {
                "decision": "need_node_access",
                "reason": "confirm which files are consuming /var",
                "target_nodes": ["overcloud-compute-0"],
            },
            "node_agent": [
                {"node": "overcloud-compute-0", "command": "df -h /var", "rationale": "confirm fill"},
                {
                    "node": "overcloud-compute-0",
                    "command": "sudo tail -n 100 /var/log/containers/nova.log",
                    "rationale": "find the writer",
                },
                {"done": True},
            ],
            "report": {
                "root_cause": "/var is 100% full; nova logs grew unbounded",
                "confidence": 0.9,
                "recommendations": ["Rotate /var/log/containers", "Alert at 80% usage"],
                "timeline": ["/var filled", "service stopped"],
            },
        }
    )
    prometheus = FakePrometheus(
        {"node_filesystem_avail_bytes": vector("node_filesystem_avail_bytes", 1048576)}
    )
    ssh = FakeSSH(
        {
            "df -h /var": "/dev/vda2  20G  20G  0  100% /var",
            "tail": "2026-08-06 ERROR disk write failed: No space left on device",
        }
    )

    reports = await scenario(
        [alarm("NodeFilesystemAlmostOutOfSpace"), alarm("ServiceDown", severity="warning")],
        gateway, prometheus, ssh,
    )

    assert len(reports) == 1
    _, report = reports[0]
    assert "100% full" in report.root_cause
    assert "100% /var" in report.markdown
    assert "No space left on device" in report.markdown
    assert [cmd for _, cmd in ssh.executed] == [
        "df -h /var",
        "sudo tail -n 100 /var/log/containers/nova.log",
    ]


# -- scenario 2: service crash-loop -----------------------------------------


async def test_service_crash_loop_collects_unit_state_and_logs(scenario):
    gateway = FakeGateway(
        {
            "planner": {
                "hypotheses": ["tripleo_nova_compute is crash-looping"],
                "checks": [{"goal": "Is the service scraping?", "metric_hint": "up"}],
            },
            "promql": {
                "queries": [{"goal": "Is the service scraping?", "promql": "up", "range_query": False}]
            },
            "reflection": [OK, OK],
            "decision": {"decision": "need_node_access", "reason": "need the unit's logs"},
            "node_agent": [
                {
                    "node": "overcloud-compute-0",
                    "command": "systemctl status tripleo_nova_compute",
                    "rationale": "unit state",
                },
                {
                    "node": "overcloud-compute-0",
                    "command": "journalctl -u tripleo_nova_compute --no-pager -n 200",
                    "rationale": "crash reason",
                },
                {"done": True},
            ],
            "report": {
                "root_cause": "tripleo_nova_compute restarts on a bad AMQP credential",
                "confidence": 0.75,
                "recommendations": ["Fix the AMQP password in the container env"],
                "timeline": [],
            },
        }
    )
    ssh = FakeSSH(
        {
            "systemctl status": "Active: activating (auto-restart) (Result: exit-code)",
            "journalctl": "AMQP authentication failure for user nova",
        }
    )

    reports = await scenario([alarm("ServiceDown")], gateway, FakePrometheus(), ssh)

    _, report = reports[0]
    assert "AMQP" in report.markdown
    assert "auto-restart" in report.markdown
    assert report.recommendations


# -- scenario 3: node down --------------------------------------------------


async def test_node_down_escalates_without_asking_the_model(scenario):
    """An exporter returning nothing is exactly the case metrics cannot resolve on their own."""
    gateway = FakeGateway(
        {
            "planner": {
                "hypotheses": ["The host is gone"],
                "checks": [{"goal": "Is the exporter up?", "metric_hint": "up"}],
            },
            "promql": {
                "queries": [{"goal": "Is the exporter up?", "promql": "up", "range_query": False}]
            },
            "reflection": [OK, OK],
            # No "decision" entry: the rule must settle this before an LLM call is attempted.
            "node_agent": [
                {"node": "overcloud-compute-0", "command": "uptime", "rationale": "is it alive?"},
                {"done": True},
            ],
            "report": {
                "root_cause": "The node rebooted",
                "confidence": 0.6,
                "recommendations": ["Check the hardware event log"],
                "timeline": [],
            },
        }
    )
    prometheus = FakePrometheus()  # every query returns an empty vector

    reports = await scenario([alarm("NodeDown")], gateway, prometheus, FakeSSH())

    _, report = reports[0]
    assert "decision" not in [name for name in gateway.agents_called()]
    assert report.data["decision"] == "need_node_access"
    assert "exporter returns nothing" in report.data["decision_reason"]


# -- scenario 4: network flap -----------------------------------------------


async def test_network_flap_is_answered_from_metrics_alone(scenario):
    gateway = FakeGateway(
        {
            "planner": {
                "hypotheses": ["The uplink is flapping"],
                "checks": [{"goal": "Carrier changes", "metric_hint": "node_network_carrier_changes_total"}],
            },
            "promql": {
                "queries": [
                    {
                        "goal": "Carrier changes",
                        "promql": "rate(node_network_carrier_changes_total[15m])",
                        "range_query": True,
                    }
                ]
            },
            "reflection": OK,
            "decision": {"decision": "sufficient", "reason": "the metric is unambiguous"},
            "report": {
                "root_cause": "The ens3 uplink flapped 40 times in 15 minutes",
                "confidence": 0.85,
                "recommendations": ["Replace the transceiver on the ToR port"],
                "timeline": [],
            },
        }
    )
    prometheus = FakePrometheus(
        {"carrier_changes": vector("node_network_carrier_changes_total", 40, device="ens3")}
    )
    ssh = FakeSSH()

    reports = await scenario([alarm("NetworkFlapping")], gateway, prometheus, ssh)

    _, report = reports[0]
    assert ssh.executed == [], "a clear metric answer must not trigger an SSH session"
    assert "flapped 40 times" in report.root_cause
    # The rate() query kept its function intact through cluster scoping.
    assert any("rate(" in q and 'site="lab"' in q for q in prometheus.queries)


# -- scenario 5: multi-node event -------------------------------------------


async def test_alarms_across_two_nodes_produce_two_investigations(scenario):
    gateway = FakeGateway(
        {
            "planner": [
                {"hypotheses": ["h"], "checks": [{"goal": "load", "metric_hint": "node_load5"}]},
                {"hypotheses": ["h"], "checks": [{"goal": "load", "metric_hint": "node_load5"}]},
            ],
            "promql": [
                {"queries": [{"goal": "load", "promql": "node_load5", "range_query": False}]},
                {"queries": [{"goal": "load", "promql": "node_load5", "range_query": False}]},
            ],
            "reflection": [OK, OK],
            "decision": [
                {"decision": "sufficient", "reason": "ok"},
                {"decision": "sufficient", "reason": "ok"},
            ],
            "report": [
                {"root_cause": "load spike on compute-0", "confidence": 0.5,
                 "recommendations": ["watch it"], "timeline": []},
                {"root_cause": "load spike on compute-1", "confidence": 0.5,
                 "recommendations": ["watch it"], "timeline": []},
            ],
        }
    )
    prometheus = FakePrometheus({"node_load5": vector("node_load5", 12.5)})

    reports = await scenario(
        [
            alarm("HighLoad", node="overcloud-compute-0"),
            alarm("HighLoad", node="overcloud-compute-1"),
        ],
        gateway, prometheus, FakeSSH(),
    )

    assert len(reports) == 2
    group_keys = {incident.group_key for incident, _ in reports}
    assert group_keys == {
        "lab-cluster-1|node=overcloud-compute-0",
        "lab-cluster-1|node=overcloud-compute-1",
    }
    assert all(report is not None for _, report in reports)


# -- scenario 6: a model that goes off the rails -----------------------------


async def test_a_model_proposing_remediation_is_contained(scenario):
    """The agent is read-only by construction: a 'helpful' fix attempt must never execute."""
    gateway = FakeGateway(
        {
            "planner": {"hypotheses": ["service wedged"], "checks": [{"goal": "up", "metric_hint": "up"}]},
            "promql": {"queries": [{"goal": "up", "promql": "up", "range_query": False}]},
            "reflection": [OK, OK],
            "decision": {"decision": "need_node_access", "reason": "check the unit"},
            "node_agent": [
                {"node": "overcloud-compute-0", "command": "systemctl restart tripleo_nova_compute",
                 "rationale": "just restart it"},
                {"node": "overcloud-compute-0", "command": "sudo podman stop nova_compute",
                 "rationale": "stop the container"},
                {"node": "overcloud-compute-0", "command": "systemctl status tripleo_nova_compute",
                 "rationale": "check state"},
            ],
            "report": {
                "root_cause": "The unit is inactive",
                "confidence": 0.5,
                "recommendations": ["Restart tripleo_nova_compute during a maintenance window"],
                "timeline": [],
            },
        }
    )
    ssh = FakeSSH({"systemctl status": "Active: inactive (dead)"})

    reports = await scenario([alarm("ServiceDown")], gateway, FakePrometheus(), ssh)

    executed = [cmd for _, cmd in ssh.executed]
    assert executed == ["systemctl status tripleo_nova_compute"]
    _, report = reports[0]
    refused = [e for e in report.data["node_evidence"] if e["result"].startswith("REFUSED")]
    assert len(refused) == 2
    # The remediation survives only as a recommendation for a human to action.
    assert "Restart tripleo_nova_compute" in report.recommendations[0]
