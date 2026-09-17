"""Application settings, runtime tunables and cluster inventory loading."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# ---------------------------------------------------------------------------
# Cluster inventory (config/clusters.yaml)
# ---------------------------------------------------------------------------


class DirectorConfig(BaseModel):
    host: str
    user: str = "troubleshoot"
    port: int = 22
    auth: str = "key"  # key | password
    key_path: str = ""  # path to private key (mounted secret)
    password_env: str = ""  # env var holding password when auth=password
    stack_user: str = "stack"  # undercloud user holding the overcloud keys
    # Empty means "derive from the detected RHOSP version": 16 uses heat-admin, 17 uses
    # tripleo-admin (renamed in 16.2). Set explicitly to override detection.
    overcloud_user: str = ""
    # Empty means detect from /etc/rhosp-release on the director. Set to "16" or "17" to skip
    # detection — useful when the release file is absent or the director is locked down.
    osp_version: str = ""
    # Domain suffix carried by alarm host labels (e.g. "rhosp2.jiocaas.com"). Stripped before
    # comparing against inventory hostnames. Empty means compare on the first DNS label.
    domain: str = ""
    # Static hostname -> ctlplane IP map. Set this when neither metalsmith nor Nova on the
    # undercloud reports the hostnames your alarms carry; it short-circuits inventory discovery.
    nodes: dict[str, str] = Field(default_factory=dict)


class PrometheusInstance(BaseModel):
    url: str
    timeout_seconds: int = 30
    # STF/OpenShift serves Prometheus over HTTPS with a service-serving certificate signed by the
    # cluster's own CA, which is not in the system trust store. Point ca_bundle at that CA rather
    # than disabling verification — verify_ssl: false is a last resort for a throwaway lab.
    ca_bundle: str = ""  # e.g. /var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt
    verify_ssl: bool = True
    bearer_token_env: str = ""  # env var holding a bearer token
    bearer_token_file: str = ""  # e.g. /var/run/secrets/kubernetes.io/serviceaccount/token


class ClusterConfig(BaseModel):
    prometheus: str  # key into prometheus_instances
    director: DirectorConfig
    labels: dict[str, str] = Field(default_factory=dict)  # alarm label matchers

    # Matchers injected into every PromQL query to scope a shared Prometheus. These are NOT the
    # same as `labels`: alarm labels come from the alerting rule, metric labels come from the
    # scrape config, and they routinely differ. Set to {} when one Prometheus serves one cluster
    # — scoping is then unnecessary, and injecting a label the metrics do not carry makes every
    # query return empty, which is indistinguishable from a healthy system.
    # Left unset, it falls back to `labels` for backwards compatibility.
    promql_matchers: dict[str, str] | None = None

    # Operator knowledge, resolved relative to config_dir when not absolute. Left unset, they
    # default to metrics/<cluster-key>.txt and promql_library.yaml if those exist.
    metrics_file: str = ""  # one metric name per line, from /api/v1/label/__name__/values
    promql_library: str = ""  # known-good queries, see config/promql_library.yaml


class ClusterInventory(BaseModel):
    prometheus_instances: dict[str, PrometheusInstance] = Field(default_factory=dict)
    clusters: dict[str, ClusterConfig] = Field(default_factory=dict)

    def resolve_cluster(self, alarm_labels: dict[str, str]) -> str | None:
        """Match alarm labels to a cluster key.

        Precedence: explicit cluster/site label equal to a cluster key,
        then per-cluster label matchers (all must match).
        """
        for label in ("cluster", "site"):
            value = alarm_labels.get(label)
            if value and value in self.clusters:
                return value
        for key, cfg in self.clusters.items():
            if cfg.labels and all(alarm_labels.get(k) == v for k, v in cfg.labels.items()):
                return key
        if len(self.clusters) == 1:  # lab POC: single cluster catch-all
            return next(iter(self.clusters))
        return None

    def prometheus_for(self, cluster_key: str) -> PrometheusInstance | None:
        cfg = self.clusters.get(cluster_key)
        if not cfg:
            return None
        return self.prometheus_instances.get(cfg.prometheus)

    def director_for(self, cluster_key: str) -> DirectorConfig | None:
        cfg = self.clusters.get(cluster_key)
        return cfg.director if cfg else None

    def knowledge_paths(self, cluster_key: str, config_dir: Path) -> tuple[Path | None, Path | None]:
        """(metrics_file, promql_library) for a cluster, or None where nothing is configured."""
        cfg = self.clusters.get(cluster_key)
        if not cfg:
            return None, None

        def resolve(configured: str, default: Path) -> Path | None:
            if configured:
                path = Path(configured)
                return path if path.is_absolute() else config_dir / path
            return default if default.exists() else None

        return (
            resolve(cfg.metrics_file, config_dir / "metrics" / f"{cluster_key}.txt"),
            resolve(cfg.promql_library, config_dir / "promql_library.yaml"),
        )

    def label_matchers_for(self, cluster_key: str) -> dict[str, str]:
        """Label matchers the PromQL agent must inject so a shared Prometheus is scoped."""
        cfg = self.clusters.get(cluster_key)
        if not cfg:
            return {}
        if cfg.promql_matchers is not None:
            return dict(cfg.promql_matchers)
        return dict(cfg.labels)


# ---------------------------------------------------------------------------
# Runtime tunables (config/settings.yaml, overridable by env)
# ---------------------------------------------------------------------------


class CorrelationConfig(BaseModel):
    window_seconds: int = 300
    max_group_age_seconds: int = 900
    sweep_interval_seconds: int = 15


class ReflectionConfig(BaseModel):
    enabled: bool = True
    max_retries: int = 2


class InvestigationConfig(BaseModel):
    max_promql_queries: int = 6
    max_node_commands: int = 8
    node_access_threshold: float = 0.7


class ReportConfig(BaseModel):
    notify_webhook: str = ""


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Feeds config/settings.yaml into AppConfig below env vars in precedence."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = get_settings().config_dir / "settings.yaml"
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}


class AppConfig(BaseSettings):
    """Runtime tunables. Env wins over settings.yaml: TSAGENT_CORRELATION__WINDOW_SECONDS=60."""

    model_config = SettingsConfigDict(
        env_prefix="TSAGENT_", env_nested_delimiter="__", extra="ignore"
    )

    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    investigation: InvestigationConfig = Field(default_factory=InvestigationConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSettingsSource(settings_cls),
            file_secret_settings,
        )


# ---------------------------------------------------------------------------
# Infrastructure settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TSAGENT_", env_file=".env", extra="ignore")

    config_dir: Path = Path("config")
    db_path: Path = Path("data/cases.db")
    checkpoint_path: Path = Path("data/checkpoints.db")

    def load_inventory(self) -> ClusterInventory:
        path = self.config_dir / "clusters.yaml"
        if not path.exists():
            return ClusterInventory()
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return ClusterInventory.model_validate(data)

    def load_app_config(self) -> AppConfig:
        return AppConfig()


@lru_cache
def get_settings() -> Settings:
    return Settings()
