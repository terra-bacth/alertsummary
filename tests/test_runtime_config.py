"""Everything environment-specific must be configurable, not hard-coded.

These pin down the configuration surface the OpenShift deployment relies on: ${ENV} placeholders
in the YAML files, the TSAGENT_* process settings, and that the tunables actually reach the code
that uses them.
"""

from __future__ import annotations

import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from tsagent.config import (
    AppConfig,
    DirectorConfig,
    Settings,
    SSHConfig,
    expand_env,
    get_settings,
    load_yaml,
)
from tsagent.events import EventBus
from tsagent.models import Report
from tsagent.reporting.notifier import notify_report
from tsagent.tools.ssh import AllowList, SSHExecutor, _parse_inventory

# -- ${ENV} placeholders ------------------------------------------------------


def test_expand_env_forms(monkeypatch):
    monkeypatch.setenv("TS_SET", "value")
    monkeypatch.setenv("TS_EMPTY", "")
    monkeypatch.delenv("TS_UNSET", raising=False)
    assert expand_env("${TS_SET}") == "value"
    assert expand_env("https://${TS_SET}:9090/x") == "https://value:9090/x"
    assert expand_env("${TS_UNSET:-fallback}") == "fallback"
    assert expand_env("${TS_EMPTY:-fallback}") == "fallback"  # :- also covers empty
    assert expand_env("${TS_EMPTY-fallback}") == ""  # - covers unset only
    assert expand_env("${TS_UNSET-fallback}") == "fallback"
    assert expand_env("${TS_UNSET}") == ""
    assert expand_env("$${TS_SET}") == "${TS_SET}"  # escape


def test_expand_env_leaves_promql_and_regex_dollars_alone():
    promql = 'label_replace(up, "node", "$1", "host", "(.+)")'
    assert expand_env(promql) == promql
    assert expand_env("^df -h$") == "^df -h$"


def test_expand_env_cannot_inject_structure(monkeypatch):
    monkeypatch.setenv("TS_EVIL", "x\nclusters: {pwned: true}")
    assert expand_env({"a": "${TS_EVIL}"}) == {"a": "x\nclusters: {pwned: true}"}


def test_expand_env_recurses_into_lists_and_dicts(monkeypatch):
    monkeypatch.setenv("TS_SET", "v")
    assert expand_env({"a": ["${TS_SET}", 1, {"b": "${TS_SET}"}], "n": 3}) == {
        "a": ["v", 1, {"b": "v"}], "n": 3,
    }


def test_load_yaml_rejects_non_mapping(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("- a\n- b\n")
    with pytest.raises(TypeError):
        load_yaml(path)


def test_clusters_yaml_placeholders_resolve_from_the_environment(tmp_path, monkeypatch):
    (tmp_path / "clusters.yaml").write_text(
        """
prometheus_instances:
  prom:
    url: "${PROM_URL}"
    timeout_seconds: "${PROM_TIMEOUT:-45}"
    verify_ssl: "${PROM_VERIFY:-true}"
clusters:
  c1:
    prometheus: prom
    director:
      host: "${DIRECTOR_HOST}"
      port: "${DIRECTOR_PORT:-22}"
"""
    )
    monkeypatch.setenv("PROM_URL", "https://thanos.example:9091")
    monkeypatch.setenv("DIRECTOR_HOST", "director.example")
    monkeypatch.setenv("PROM_VERIFY", "false")
    inventory = Settings(config_dir=tmp_path).load_inventory()
    prom = inventory.prometheus_for("c1")
    assert prom.url == "https://thanos.example:9091"
    assert prom.timeout_seconds == 45
    assert prom.verify_ssl is False
    director = inventory.director_for("c1")
    assert director.host == "director.example"
    assert director.port == 22


def test_llm_yaml_placeholders_resolve(tmp_path, monkeypatch):
    from tsagent.llm import LLMGateway

    (tmp_path / "llm.yaml").write_text(
        """
default_profile: local
profiles:
  local:
    model: hosted_vllm/qwen
    api_base: "${LLM_BASE:-http://vllm:8000/v1}"
    timeout_seconds: "${LLM_TIMEOUT:-60}"
"""
    )
    monkeypatch.setenv("LLM_BASE", "https://llm.apps.example/v1")
    gateway = LLMGateway(tmp_path / "llm.yaml")
    assert gateway.profiles["local"].api_base == "https://llm.apps.example/v1"
    assert gateway.profiles["local"].timeout_seconds == 60


def test_allowlist_is_never_env_expanded(tmp_path, monkeypatch):
    """The security boundary must be exactly what was reviewed."""
    monkeypatch.setenv("EVIL", ".*")
    (tmp_path / "ssh_allowlist.yaml").write_text(
        "forbidden_characters: []\ndirector: []\n"
        "overcloud:\n  - pattern: '^uptime${EVIL}$'\n    description: x\n"
    )
    allowlist = AllowList.load(tmp_path / "ssh_allowlist.yaml")
    assert allowlist.overcloud[0][0].pattern == "^uptime${EVIL}$"


# -- tunables -----------------------------------------------------------------


def test_new_sections_have_safe_defaults():
    config = AppConfig()
    assert config.ssh.command_timeout_seconds == 120
    assert config.ssh.known_hosts == ""
    assert config.events.buffer_size == 4000
    assert config.orchestrator.resume_on_start is True
    assert config.demo.enabled is True
    assert config.report.max_tokens == 900


def test_new_sections_are_env_overridable(monkeypatch):
    monkeypatch.setenv("TSAGENT_SSH__COMMAND_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("TSAGENT_SSH__KNOWN_HOSTS", "/etc/tsagent/ssh/known_hosts")
    monkeypatch.setenv("TSAGENT_DEMO__ENABLED", "false")
    monkeypatch.setenv("TSAGENT_EVENTS__SUBSCRIBER_QUEUE_SIZE", "64")
    monkeypatch.setenv("TSAGENT_REPORT__EVIDENCE_ITEMS", "12")
    config = AppConfig()
    assert config.ssh.command_timeout_seconds == 300
    assert config.ssh.known_hosts == "/etc/tsagent/ssh/known_hosts"
    assert config.demo.enabled is False
    assert config.events.subscriber_queue_size == 64
    assert config.report.evidence_items == 12


def test_settings_yaml_placeholders(tmp_path, monkeypatch):
    (tmp_path / "settings.yaml").write_text(
        "report:\n  notify_webhook: '${TEAMS_WEBHOOK:-}'\n"
        "correlation:\n  window_seconds: '${WINDOW:-120}'\n"
    )
    monkeypatch.setenv("TSAGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("TEAMS_WEBHOOK", "https://hooks.example/abc")
    get_settings.cache_clear()
    try:
        config = AppConfig()
    finally:
        get_settings.cache_clear()
    assert config.report.notify_webhook == "https://hooks.example/abc"
    assert config.correlation.window_seconds == 120


def test_process_settings_from_env(monkeypatch):
    monkeypatch.setenv("TSAGENT_HOST", "0.0.0.0")
    monkeypatch.setenv("TSAGENT_PORT", "9000")
    monkeypatch.setenv("TSAGENT_LOG_FORMAT", "json")
    monkeypatch.setenv("TSAGENT_CORS_ORIGINS", "https://a.example, https://b.example")
    settings = Settings()
    assert (settings.host, settings.port, settings.log_format) == ("0.0.0.0", 9000, "json")
    assert settings.cors_origin_list == ["https://a.example", "https://b.example"]


def test_empty_cors_falls_back_to_open():
    assert Settings(cors_origins="").cors_origin_list == ["*"]


def test_event_bus_queue_size_is_configurable():
    bus = EventBus(queue_size=3)
    assert bus.subscribe().maxsize == 3
    assert bus.subscribe(maxsize=7).maxsize == 7


# -- SSH ----------------------------------------------------------------------


async def test_ssh_uses_configured_timeouts_and_known_hosts(monkeypatch, tmp_path):
    import asyncssh

    captured: dict = {}

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def run(self, command, check, timeout):
            captured["timeout"] = timeout

            class R:
                exit_status = 0
                stdout = "ok"
                stderr = ""

            return R()

    async def fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConn()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    director = DirectorConfig(host="d", key_path=str(tmp_path / "k"))
    ssh = SSHExecutor(
        director,
        AllowList([], [], []),
        ssh_config=SSHConfig(
            connect_timeout_seconds=7,
            command_timeout_seconds=99,
            keepalive_interval_seconds=15,
            known_hosts="/etc/tsagent/ssh/known_hosts",
        ),
    )
    result = await ssh._run_raw("uptime", "director")
    assert result.ok
    assert captured["connect_timeout"] == 7
    assert captured["timeout"] == 99
    assert captured["keepalive_interval"] == 15
    assert captured["known_hosts"] == "/etc/tsagent/ssh/known_hosts"


async def test_director_known_hosts_wins_and_empty_disables(monkeypatch, tmp_path):
    import asyncssh

    seen: list = []

    async def fake_connect(**kwargs):
        seen.append(kwargs["known_hosts"])
        raise OSError("stop here")

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    allow = AllowList([], [], [])
    own = DirectorConfig(host="d", key_path="k", known_hosts="/own")
    await SSHExecutor(own, allow, ssh_config=SSHConfig(known_hosts="/global"))._run_raw("x", "d")
    await SSHExecutor(DirectorConfig(host="d", key_path="k"), allow)._run_raw("x", "d")
    assert seen == ["/own", None]


def test_metalsmith_inventory_with_unparseable_ctlplane_does_not_crash():
    # Previously a NameError (``adrs``) on exactly this shape.
    stdout = '{"ctrl-0": {"hostname": "ctrl-0", "ip_addresses": {"ctlplane": ["not-an-ip"]}}}'
    assert _parse_inventory(stdout) == {}


# -- notifier -------------------------------------------------------------------


@respx.mock
async def test_notifier_sends_bearer_token_from_env(monkeypatch):
    monkeypatch.setenv("HOOK_TOKEN", "s3cret")
    route = respx.post("https://hooks.example/x").mock(return_value=Response(200))
    report = Report(incident_id="inc-1", root_cause="disk full", confidence=0.9)
    assert await notify_report("https://hooks.example/x", report, token_env="HOOK_TOKEN")
    assert route.calls.last.request.headers["authorization"] == "Bearer s3cret"


@respx.mock
async def test_notifier_without_token_sends_no_auth_header():
    route = respx.post("https://hooks.example/x").mock(return_value=Response(200))
    report = Report(incident_id="inc-1", root_cause="disk full", confidence=0.9)
    assert await notify_report("https://hooks.example/x", report)
    assert "authorization" not in route.calls.last.request.headers


# -- API / UI -------------------------------------------------------------------


async def test_demo_disabled_is_a_clear_503(client, monkeypatch):
    from tsagent.main import app

    monkeypatch.setattr(app.state, "demo", None, raising=False)
    response = await client.post("/api/demo")
    assert response.status_code == 503
    assert "demo.enabled" in response.json()["detail"]


async def test_ui_is_served_same_origin_without_shadowing_the_api(tmp_path):
    from tsagent.main import mount_ui

    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    ui_app = FastAPI()

    @ui_app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    assert mount_ui(ui_app, str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://t") as c:
        assert "dashboard" in (await c.get("/")).text
        assert (await c.get("/healthz")).json() == {"status": "ok"}


def test_ui_mount_is_skipped_when_unset_or_missing(tmp_path):
    from tsagent.main import mount_ui

    assert not mount_ui(FastAPI(), "")
    assert not mount_ui(FastAPI(), str(tmp_path / "nope"))


def test_route_path_prefix_serves_prefixed_and_bare_paths(tmp_path):
    """OpenShift's router forwards a Route's path prefix unchanged; probes use the bare path.

    Runs in a subprocess because TSAGENT_ROOT_PATH is read when tsagent.main is imported.
    """
    import os
    import subprocess
    import sys

    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    script = """
import asyncio
from httpx import ASGITransport, AsyncClient
from tsagent.main import app

async def main():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        codes = [
            (await c.get(p)).status_code
            for p in ("/healthz", "/tsagent/healthz", "/tsagent", "/tsagent/")
        ]
        print(codes)

asyncio.run(main())
"""
    env = {**os.environ, "TSAGENT_ROOT_PATH": "/tsagent", "TSAGENT_UI_DIR": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip().splitlines()[-1] == "[200, 200, 308, 200]"
