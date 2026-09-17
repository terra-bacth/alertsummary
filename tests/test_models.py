"""Tests for domain models and cluster resolution."""

from tsagent.config import ClusterInventory
from tsagent.models import Alarm, AMAlert, AMWebhook


def test_webhook_parses():
    from tests.conftest import SAMPLE_WEBHOOK

    wh = AMWebhook.model_validate(SAMPLE_WEBHOOK)
    assert wh.version == "4"
    assert len(wh.alerts) == 2
    assert wh.alerts[0].labels["alertname"] == "NodeDown"


def test_alarm_normalization():
    am = AMAlert(
        status="firing",
        labels={
            "alertname": "NodeDown",
            "severity": "critical",
            "instance": "compute-0:9100",
            "site": "lab",
        },
        fingerprint="fp1",
    )
    alarm = Alarm.from_am_alert(am)
    assert alarm.name == "NodeDown"
    assert alarm.node == "compute-0"  # port stripped
    assert alarm.site == "lab"
    assert alarm.severity == "critical"
    assert alarm.fingerprint == "fp1"


def test_fingerprint_computed_when_missing():
    am = AMAlert(labels={"alertname": "X", "instance": "n1"})
    a1 = am.computed_fingerprint()
    a2 = AMAlert(labels={"alertname": "X", "instance": "n1"}).computed_fingerprint()
    assert a1 == a2 and len(a1) == 16


def test_cluster_resolution(inventory: ClusterInventory):
    # via label matchers
    assert inventory.resolve_cluster({"site": "lab"}) == "lab-cluster-1"
    # via explicit cluster label equal to key
    assert inventory.resolve_cluster({"cluster": "lab-cluster-1"}) == "lab-cluster-1"
    # single-cluster catch-all
    assert inventory.resolve_cluster({"foo": "bar"}) == "lab-cluster-1"


def test_prometheus_for_cluster(inventory: ClusterInventory):
    prom = inventory.prometheus_for("lab-cluster-1")
    assert prom is not None and prom.url == "http://prom:9090"
    assert inventory.prometheus_for("nope") is None


def test_multi_cluster_no_catchall():
    inv = ClusterInventory.model_validate(
        {
            "prometheus_instances": {"p1": {"url": "http://p1:9090"}},
            "clusters": {
                "c1": {"prometheus": "p1", "director": {"host": "d1"}, "labels": {"site": "s1"}},
                "c2": {"prometheus": "p1", "director": {"host": "d2"}, "labels": {"site": "s2"}},
            },
        }
    )
    assert inv.resolve_cluster({"site": "s2"}) == "c2"
    assert inv.resolve_cluster({"site": "unknown"}) is None
