"""Shared pytest fixtures.

Nothing here talks to a real LLM, Prometheus or node. FakeGateway scripts model responses per
agent so the graph's control flow can be tested deterministically, which is the part that
actually decides whether an operator gets a useful report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tsagent.config import AppConfig, ClusterInventory
from tsagent.correlation import CorrelationEngine
from tsagent.graph import GraphDeps, Orchestrator
from tsagent.knowledge import Knowledge
from tsagent.llm import LLMError, LLMResponse
from tsagent.store import Repository
from tsagent.tools.prometheus import QueryResult
from tsagent.tools.ssh import AllowList

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGateway:
    """Scripted stand-in for LLMGateway.

    ``responses`` maps an agent name to either a single payload or a list consumed in order.
    A payload may be a dict/BaseModel (returned as JSON) or a string. Agents with no entry
    raise LLMError, which is how the degraded paths get exercised.
    """

    def __init__(self, responses: dict[str, object] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, list[dict]]] = []

    def _next(self, agent_name: str):
        if agent_name not in self.responses:
            raise LLMError(f"no scripted response for agent {agent_name!r}")
        entry = self.responses[agent_name]
        if isinstance(entry, list):
            if not entry:
                raise LLMError(f"scripted responses for {agent_name!r} exhausted")
            return entry.pop(0)
        return entry

    async def complete(self, agent_name, messages, tools=None, max_tokens=None, json_mode=False):
        self.calls.append((agent_name, messages))
        payload = self._next(agent_name)
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(
            content=content, model="fake", profile="fake", latency_ms=1
        )

    async def complete_json(self, agent_name, messages, schema, max_tokens=None):
        self.calls.append((agent_name, messages))
        payload = self._next(agent_name)
        if isinstance(payload, str):
            payload = json.loads(payload)
        return schema.model_validate(payload)

    def agents_called(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakePrometheus:
    """Canned Prometheus. ``results`` maps a substring of the query to a QueryResult."""

    def __init__(self, results: dict[str, QueryResult] | None = None, default_empty: bool = True):
        self.results = results or {}
        self.default_empty = default_empty
        self.queries: list[str] = []

    def _lookup(self, expr: str) -> QueryResult:
        self.queries.append(expr)
        for needle, result in self.results.items():
            if needle in expr:
                return QueryResult(
                    expr=expr,
                    ok=result.ok,
                    result_type=result.result_type,
                    series=result.series,
                    error=result.error,
                    unreachable=result.unreachable,
                )
        if self.default_empty:
            return QueryResult(expr=expr, ok=True, result_type="vector", series=[])
        return QueryResult(expr=expr, ok=False, error="no canned response")

    async def query(self, expr, at=None):
        return self._lookup(expr)

    async def query_range(self, expr, start=None, end=None, step="60s"):
        return self._lookup(expr)

    async def check_syntax(self, expr):
        return self._lookup(expr)


class FakeSSH:
    """Canned director/node executor. ``outputs`` maps a command substring to stdout."""

    def __init__(self, outputs: dict[str, str] | None = None, allowlist: AllowList | None = None):
        self.outputs = outputs or {}
        self.allowlist = allowlist or AllowList.load(CONFIG_DIR / "ssh_allowlist.yaml")
        self.executed: list[tuple[str, str]] = []
        self.nodes = {"overcloud-compute-0": "192.168.24.10", "overcloud-controller-0": "192.168.24.11"}

    async def resolve_node(self, node: str) -> str | None:
        short = node.split(".")[0]
        return self.nodes.get(short)

    async def list_nodes(self, refresh: bool = False) -> dict[str, str]:
        return self.nodes

    async def run_node(self, node: str, command: str):
        from tsagent.tools.ssh import CommandNotAllowed, CommandResult

        validated = self.allowlist.validate(command, scope="overcloud")
        if await self.resolve_node(node) is None:
            raise CommandNotAllowed(f"node {node!r} is not in the overcloud inventory")
        self.executed.append((node, validated))
        for needle, stdout in self.outputs.items():
            if needle in validated:
                return CommandResult(
                    command=validated, target=node, ok=True, stdout=stdout
                )
        return CommandResult(command=validated, target=node, ok=True, stdout="(no output)")

    async def run_director(self, command: str):
        from tsagent.tools.ssh import CommandResult

        validated = self.allowlist.validate(command, scope="director")
        self.executed.append(("director", validated))
        return CommandResult(command=validated, target="director", ok=True, stdout="")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo(tmp_path):
    r = Repository(tmp_path / "test.db")
    await r.connect()
    yield r
    await r.close()


@pytest.fixture
def inventory() -> ClusterInventory:
    return ClusterInventory.model_validate(
        {
            "prometheus_instances": {"prom-lab": {"url": "http://prom:9090"}},
            "clusters": {
                "lab-cluster-1": {
                    "prometheus": "prom-lab",
                    "director": {
                        "host": "director.lab",
                        "user": "troubleshoot",
                        "key_path": "/etc/tsagent/ssh/id_ed25519",
                    },
                    "labels": {"site": "lab"},
                }
            },
        }
    )


@pytest.fixture
def app_config() -> AppConfig:
    # Constructed explicitly so a stray settings.yaml or env var cannot change test behaviour.
    return AppConfig.model_validate(
        {
            "correlation": {
                "window_seconds": 300,
                "max_group_age_seconds": 900,
                "sweep_interval_seconds": 1,
            },
            "reflection": {"enabled": True, "max_retries": 2},
            "investigation": {"max_promql_queries": 4, "max_node_commands": 3},
            "report": {"notify_webhook": ""},
        }
    )


@pytest.fixture
def allowlist() -> AllowList:
    return AllowList.load(CONFIG_DIR / "ssh_allowlist.yaml")


@pytest.fixture
def make_deps(inventory, app_config, allowlist):
    """Factory for GraphDeps wired to fakes."""

    def _make(gateway=None, prometheus=None, ssh=None, repo=None, knowledge=None):
        # Knowledge defaults to empty so tests stay hermetic — an unconfigured catalogue must
        # not validate anything, which is also the behaviour on a fresh install.
        empty = knowledge or Knowledge()
        return GraphDeps(
            gateway=gateway or FakeGateway(),
            inventory=inventory,
            app_config=app_config,
            allowlist=allowlist,
            repo=repo,
            prometheus_factory=(lambda _cluster: prometheus),
            ssh_factory=(lambda _cluster: ssh),
            knowledge_factory=(lambda _cluster: empty),
        )

    return _make


@pytest.fixture
async def client(tmp_path, inventory, app_config, allowlist):
    """App test client with an isolated DB, a correlator and a no-LLM orchestrator."""
    from tsagent.main import app

    repo = Repository(tmp_path / "api.db")
    await repo.connect()

    deps = GraphDeps(
        gateway=FakeGateway(),
        inventory=inventory,
        app_config=app_config,
        allowlist=allowlist,
        repo=repo,
        prometheus_factory=lambda _c: None,
        ssh_factory=lambda _c: None,
        knowledge_factory=lambda _c: Knowledge(),
    )
    orchestrator = Orchestrator(deps, checkpoint_path=None, use_checkpointer=False)
    await orchestrator.start()
    correlator = CorrelationEngine(
        config=app_config.correlation,
        inventory=inventory,
        repo=repo,
        on_incident=orchestrator.handle_incident,
    )

    app.state.repo = repo
    app.state.inventory = inventory
    app.state.app_config = app_config
    app.state.deps = deps
    app.state.orchestrator = orchestrator
    app.state.correlator = correlator

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await orchestrator.stop()
    await repo.close()


SAMPLE_WEBHOOK = {
    "version": "4",
    "groupKey": '{}:{alertname="NodeDown"}',
    "status": "firing",
    "receiver": "tsagent",
    "groupLabels": {"alertname": "NodeDown"},
    "commonLabels": {"alertname": "NodeDown", "severity": "critical"},
    "commonAnnotations": {},
    "externalURL": "http://alertmanager:9093",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "NodeDown",
                "severity": "critical",
                "instance": "overcloud-compute-0.lab:9100",
                "site": "lab",
            },
            "annotations": {"summary": "Node exporter down on overcloud-compute-0"},
            "startsAt": "2026-08-05T10:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus/graph?g0.expr=up",
            "fingerprint": "abc123def456",
        },
        {
            "status": "firing",
            "labels": {
                "alertname": "HighLoad",
                "severity": "warning",
                "instance": "overcloud-compute-0.lab:9100",
                "site": "lab",
            },
            "annotations": {"summary": "High load on overcloud-compute-0"},
            "startsAt": "2026-08-05T10:01:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "",
            "fingerprint": "fed654cba321",
        },
    ],
}
