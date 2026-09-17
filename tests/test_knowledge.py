"""Metric catalogue and PromQL query library.

The goals used here are the verbatim ones the planner produced during lab testing, where every
generated query named a metric that does not exist and came back as "empty result" — which reads
exactly like a healthy system.
"""

from __future__ import annotations

import pytest
import yaml

from tests.conftest import CONFIG_DIR
from tsagent.knowledge import Knowledge, MetricCatalogue, PromQLLibrary
from tsagent.tools.prometheus import metric_names_in

# Verbatim from the lab report.
LAB_GOALS = [
    "Verify current hugepage usage across all nodes",
    "Check if any node is running out of hugepages",
    "Examine nova-compute service status for any issues",
    "Monitor memory usage across all nodes",
    "Check if any node is experiencing high CPU usage",
]

CATALOGUE = [
    "ALERTS",
    "collectd_cpu_percent",
    "collectd_df_df_complex",
    "collectd_hugepages_vmpage_number",
    "collectd_memory",
    "collectd_uptime",
    "sensubility_container_health_status",
    "up",
]


@pytest.fixture
def library() -> PromQLLibrary:
    return PromQLLibrary.load(CONFIG_DIR / "promql_library.yaml")


@pytest.fixture
def catalogue() -> MetricCatalogue:
    return MetricCatalogue(CATALOGUE)


# -- metric extraction ------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("up", ["up"]),
        ('up{job="x"}', ["up"]),
        ("rate(collectd_cpu_percent[5m])", ["collectd_cpu_percent"]),
        ("sum by (host) (collectd_memory)", ["collectd_memory"]),
        (
            (
                'sum by (host) (collectd_df_df_complex{type_instance="used"}) / '
                "sum by (host) (collectd_df_df_complex)"
            ),
            ["collectd_df_df_complex"],
        ),
        ("min_over_time(sensubility_container_health_status[5m])",
         ["sensubility_container_health_status"]),
    ],
)
def test_metric_names_are_extracted(expr, expected):
    assert metric_names_in(expr) == expected


def test_grouping_labels_are_not_metrics():
    assert "host" not in metric_names_in("sum by (host) (collectd_memory)")


def test_label_values_are_not_metrics():
    assert metric_names_in('collectd_cpu_percent{type_instance="idle"}') == ["collectd_cpu_percent"]


# -- library search ---------------------------------------------------------


def test_library_loads(library):
    assert len(library) > 20


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Verify current hugepage usage across all nodes", "hugepage_utilisation%"),
        ("Monitor memory usage across all nodes", "mem_utilization%"),
        ("Check if any node is experiencing high CPU usage", "cpu_utilization%"),
        ("Examine nova-compute service status for any issues", "health_nova_compute"),
        ("check disk space", "disk_utilization%"),
    ],
)
def test_lab_goals_reach_the_right_library_entry(library, goal, expected):
    """Every one of these produced an invented metric name in the lab."""
    assert expected in [e.name for e in library.search(goal, limit=3)]


def test_search_returns_nothing_for_an_unrelated_goal(library):
    assert library.search("what is the airspeed velocity of a swallow") == []


def test_library_entries_expose_real_metric_names(library):
    entry = next(e for e in library.entries if e.name == "hugepage_utilisation%")
    assert entry.metrics == ["collectd_hugepages_vmpage_number"]
    # The metric_names field is a token dump and must not be trusted for validation.
    assert "by" in entry.keywords
    assert "by" not in entry.metrics


def test_search_many_dedupes_across_goals(library):
    entries = library.search_many(LAB_GOALS, limit=8)
    names = [e.name for e in entries]
    assert len(names) == len(set(names))
    assert "hugepage_utilisation%" in names


def test_missing_library_is_empty_not_an_error(tmp_path):
    assert len(PromQLLibrary.load(tmp_path / "nope.yaml")) == 0


def test_malformed_library_is_skipped(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("queries: [this is not a mapping")
    assert len(PromQLLibrary.load(path)) == 0


def test_entry_without_promql_is_skipped(tmp_path):
    path = tmp_path / "lib.yaml"
    path.write_text(yaml.safe_dump({"queries": {"broken": {"metric_names": ["x"]}}}))
    assert len(PromQLLibrary.load(path)) == 0


# -- catalogue --------------------------------------------------------------


def test_catalogue_loads_from_a_file(tmp_path):
    path = tmp_path / "metrics.txt"
    path.write_text("# a comment\ncollectd_memory\n\ncollectd_uptime\n")
    catalogue = MetricCatalogue.load(path)
    assert len(catalogue) == 2
    assert catalogue.exists("collectd_memory")


def test_hallucinated_metric_is_flagged(catalogue):
    """This is the exact query the lab run executed against a healthy Prometheus."""
    unknown = catalogue.unknown_metrics('sum(node_hugepages_used{site="lab"})')
    assert unknown == ["node_hugepages_used"]


def test_real_metric_passes(catalogue):
    good = '100 * sum by (host) (collectd_hugepages_vmpage_number{type_instance="used"})'
    assert catalogue.unknown_metrics(good) == []


def test_suggestions_point_at_the_right_metric(catalogue):
    assert "collectd_hugepages_vmpage_number" in catalogue.suggest("node_hugepages_used")


def test_an_empty_catalogue_validates_nothing(tmp_path):
    """An unconfigured catalogue must not block queries — that is the fresh-install path."""
    assert MetricCatalogue().unknown_metrics("anything_at_all") == []


def test_search_never_dumps_the_whole_catalogue():
    big = MetricCatalogue([f"collectd_metric_{i}" for i in range(5000)])
    assert len(big.search("collectd metric", limit=40)) == 40


# -- combined prompt context ------------------------------------------------


def test_prompt_context_includes_examples_and_metric_names(library, catalogue):
    knowledge = Knowledge(catalogue=catalogue, library=library)
    context = knowledge.prompt_context(LAB_GOALS)
    assert "Known-good queries" in context
    assert "collectd_hugepages_vmpage_number" in context
    assert "use ONLY names from this list" in context


def test_prompt_context_is_empty_without_knowledge():
    assert Knowledge().prompt_context(LAB_GOALS) == ""


def test_prompt_context_stays_small(library, catalogue):
    """An 8B model on CPU cannot afford a full metric dump in every prompt."""
    knowledge = Knowledge(catalogue=MetricCatalogue(CATALOGUE * 500), library=library)
    assert len(knowledge.prompt_context(LAB_GOALS)) < 4000
