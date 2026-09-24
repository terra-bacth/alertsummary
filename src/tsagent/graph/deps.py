"""Collaborators the graph nodes need, bundled so tests can substitute fakes."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tsagent.config import AppConfig, ClusterInventory
from tsagent.knowledge import Knowledge
from tsagent.llm import LLMGateway
from tsagent.store import Repository
from tsagent.tools.prometheus import PrometheusClient
from tsagent.tools.ssh import AllowList, SSHExecutor


@dataclass
class GraphDeps:
    gateway: LLMGateway
    inventory: ClusterInventory
    app_config: AppConfig
    allowlist: AllowList
    repo: Repository | None = None
    config_dir: Path = Path("config")
    events: object | None = None  # EventCollector, when the UI/observability layer is enabled
    prometheus_factory: Callable[[str], PrometheusClient | None] | None = None
    ssh_factory: Callable[[str], SSHExecutor | None] | None = None
    knowledge_factory: Callable[[str], Knowledge] | None = None
    _knowledge_cache: dict[str, Knowledge] = field(default_factory=dict, repr=False)

    def knowledge_for(self, cluster: str) -> Knowledge:
        """Metric catalogue and query library for a cluster, loaded once per process."""
        if self.knowledge_factory:
            return self.knowledge_factory(cluster)
        if cluster not in self._knowledge_cache:
            metrics_file, library_file = self.inventory.knowledge_paths(cluster, self.config_dir)
            self._knowledge_cache[cluster] = Knowledge.load(metrics_file, library_file)
        return self._knowledge_cache[cluster]

    def prometheus_for(self, cluster: str) -> PrometheusClient | None:
        if self.prometheus_factory:
            return self.prometheus_factory(cluster)
        instance = self.inventory.prometheus_for(cluster)
        if not instance:
            return None
        return PrometheusClient.from_config(instance)

    def ssh_for(self, cluster: str) -> SSHExecutor | None:
        if self.ssh_factory:
            return self.ssh_factory(cluster)
        director = self.inventory.director_for(cluster)
        if not director:
            return None
        password = os.environ.get(director.password_env) if director.password_env else None
        return SSHExecutor(director, self.allowlist, password=password)
