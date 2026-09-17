"""Real Service Telemetry Framework payloads.

Captured verbatim from the lab AlertManager. STF forwards the Alertmanager *API v2* alert shape
rather than the classic webhook v4 body, which the ingest layer originally rejected with a 422 —
so these are pinned as regression tests against the exact bytes that failed.
"""

from __future__ import annotations

import pytest

from tsagent.correlation import CorrelationEngine
from tsagent.models import Alarm, AMWebhook, extract_node, utcnow

STF_WEBHOOK = {
    "receiver": "stf",
    "status": "firing",
    "alerts": [
        {
            "annotations": {"summary": "Hugepages (major)"},
            "endsAt": "2026-07-31T05:42:50.702Z",
            "fingerprint": "11a0b2a5c01a4af5",
            "receivers": [{"name": "null"}],
            "startsAt": "2026-07-31T00:49:20.702Z",
            "status": {"inhibitedBy": [], "silencedBy": [], "state": "active"},
            "updatedAt": "2026-07-31T05:38:51.093Z",
            "generatorURL": (
                "http://prometheus-example-0:9090/graph?g0.expr=sum+without+%28type_instance%29+"
                "%28collectd_hugepages_vmpage_number%7Btype_instance%3D%22free%22%7D%29+%2F+sum+"
                "without+%28type_instance%29+%28collectd_hugepages_vmpage_number%29+%3C+0.1&g0.tab=1"
            ),
            "labels": {
                "alertname": "hugepages",
                "container": "sg-core",
                "endpoint": "prom-https",
                "host": "mhngprdc0204021430comp02.rhosp2.jiocaas.com",
                "plugin_instance": "node0-1048576Kb",
                "prometheus": "service-telemetry/example",
                "service": "example-cloud1-coll-meter",
                "severity": "major",
            },
        },
        {
            "annotations": {"summary": "Hugepage usage exceeds 95%"},
            "endsAt": "2026-07-31T05:42:03.242Z",
            "fingerprint": "149b8ecf1354ef01",
            "receivers": [{"name": "null"}],
            "startsAt": "2026-07-30T23:27:33.242Z",
            "status": {"inhibitedBy": [], "silencedBy": [], "state": "active"},
            "updatedAt": "2026-07-31T05:38:03.410Z",
            "generatorURL": (
                "http://prometheus-example-0:9090/graph?g0.expr=sum+without+%28type_instance%29+"
                "%28collectd_hugepages_vmpage_number%7Bplugin_instance%3D%22mm-1048576Kb%22%2C"
                "type_instance%3D%22free%22%7D%29+%2F+sum+without+%28type_instance%29+"
                "%28collectd_hugepages_vmpage_number%29+%3C+0.05&g0.tab=1"
            ),
            "labels": {
                "alertname": "High Hugepage Usage",
                "container": "sg-core",
                "endpoint": "prom-https",
                "host": "mhngprdc0204021330comp10.rhosp2.jiocaas.com",
                "plugin_instance": "mm-1048576Kb",
                "prometheus": "service-telemetry/example",
                "service": "example-cloud1-coll-meter",
                "severity": "critical",
            },
        },
    ],
}


# -- parsing ----------------------------------------------------------------


def test_stf_payload_parses():
    webhook = AMWebhook.model_validate(STF_WEBHOOK)
    assert len(webhook.alerts) == 2


def test_object_status_is_read_as_firing():
    """API v2 sends status as an object; the classic string form must keep working too."""
    webhook = AMWebhook.model_validate(STF_WEBHOOK)
    assert all(a.status == "firing" for a in webhook.alerts)
    assert not any(a.suppressed for a in webhook.alerts)


def test_string_status_still_works():
    webhook = AMWebhook.model_validate(
        {"alerts": [{"status": "resolved", "labels": {"alertname": "X"}}]}
    )
    assert webhook.alerts[0].status == "resolved"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("active", "firing"), ("suppressed", "firing"), ("resolved", "resolved"), ("", "firing")],
)
def test_api_v2_states_map_to_firing_or_resolved(state, expected):
    webhook = AMWebhook.model_validate(
        {"alerts": [{"status": {"state": state}, "labels": {"alertname": "X"}}]}
    )
    assert webhook.alerts[0].status == expected


def test_silenced_alarm_is_marked_suppressed():
    webhook = AMWebhook.model_validate(
        {
            "alerts": [
                {
                    "status": {"state": "suppressed", "silencedBy": ["abc"], "inhibitedBy": []},
                    "labels": {"alertname": "X"},
                }
            ]
        }
    )
    assert webhook.alerts[0].suppressed is True


def test_extra_api_v2_fields_are_ignored():
    """`receivers` and `updatedAt` have no internal meaning and must not cause a 422."""
    webhook = AMWebhook.model_validate(STF_WEBHOOK)
    assert webhook.alerts[0].fingerprint == "11a0b2a5c01a4af5"


# -- host extraction --------------------------------------------------------


def test_host_label_becomes_the_node():
    alarms = [Alarm.from_am_alert(a) for a in AMWebhook.model_validate(STF_WEBHOOK).alerts]
    assert alarms[0].node == "mhngprdc0204021430comp02.rhosp2.jiocaas.com"
    assert alarms[1].node == "mhngprdc0204021330comp10.rhosp2.jiocaas.com"


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"instance": "compute-0:9100"}, "compute-0"),
        ({"host": "compute-0.example.com"}, "compute-0.example.com"),
        ({"hostname": "compute-1"}, "compute-1"),
        ({"nodename": "compute-2"}, "compute-2"),
        ({"fqdn": "compute-3.lab"}, "compute-3.lab"),
        ({"exported_instance": "compute-4:9100"}, "compute-4"),
        # instance wins when several are present
        ({"instance": "compute-0:9100", "host": "other"}, "compute-0"),
        ({"alertname": "X"}, ""),
        ({"host": "  "}, ""),
        ({"instance": "[fe80::1]:9100"}, "fe80::1"),
    ],
)
def test_node_label_precedence(labels, expected):
    assert extract_node(labels) == expected


# -- rule expression --------------------------------------------------------


def test_rule_expr_is_recovered_from_generator_url():
    alert = AMWebhook.model_validate(STF_WEBHOOK).alerts[0]
    expr = alert.rule_expr()
    assert "collectd_hugepages_vmpage_number" in expr
    assert 'type_instance="free"' in expr
    assert "< 0.1" in expr


def test_rule_expr_is_empty_without_a_generator_url():
    webhook = AMWebhook.model_validate({"alerts": [{"labels": {"alertname": "X"}}]})
    assert webhook.alerts[0].rule_expr() == ""


def test_rule_expr_survives_a_junk_url():
    webhook = AMWebhook.model_validate(
        {"alerts": [{"labels": {"alertname": "X"}, "generatorURL": "not a url at all"}]}
    )
    assert webhook.alerts[0].rule_expr() == ""


def test_metric_namespace_is_derived_for_the_promql_agent():
    from tsagent.graph.context import metric_namespaces

    alarms = [
        Alarm.from_am_alert(a).model_dump(mode="json")
        for a in AMWebhook.model_validate(STF_WEBHOOK).alerts
    ]
    assert "collectd_" in metric_namespaces(alarms)


# -- correlation ------------------------------------------------------------


async def test_the_two_hugepage_alarms_are_separate_incidents(app_config, inventory):
    """They fired on different hosts (comp02 and comp10) and must not be merged."""
    engine = CorrelationEngine(config=app_config.correlation, inventory=inventory)
    now = utcnow()
    for am_alert in AMWebhook.model_validate(STF_WEBHOOK).alerts:
        await engine.add_alarm(Alarm.from_am_alert(am_alert), now=now)

    incidents = await engine.sweep(now=now.replace(year=now.year + 1))

    assert len(incidents) == 2
    assert {i.group_key for i in incidents} == {
        "lab-cluster-1|node=mhngprdc0204021430comp02.rhosp2.jiocaas.com",
        "lab-cluster-1|node=mhngprdc0204021330comp10.rhosp2.jiocaas.com",
    }


async def test_full_ingest_path_accepts_the_stf_payload(client):
    """The exact body that returned 422 before must now be accepted and correlated."""
    response = await client.post("/alerts", json=STF_WEBHOOK)
    assert response.status_code == 202
    assert response.json()["accepted"] == 2

    alarms = (await client.get("/alarms")).json()
    assert {a["node"] for a in alarms} == {
        "mhngprdc0204021430comp02.rhosp2.jiocaas.com",
        "mhngprdc0204021330comp10.rhosp2.jiocaas.com",
    }
    assert all(a["rule_expr"] for a in alarms)

    groups = (await client.get("/correlation/pending")).json()["groups"]
    assert len(groups) == 2
