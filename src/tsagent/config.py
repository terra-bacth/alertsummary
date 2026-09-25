"""Application settings, runtime tunables and cluster inventory loading."""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML loading with ${ENV} placeholders
# ---------------------------------------------------------------------------

# ${NAME}, ${NAME:-default} (default when unset OR empty), ${NAME-default} (default when unset).
# $${...} is an escape for a literal "${...}". Only this braced form is recognised, so PromQL's
# "$1" capture references and regex "$" anchors pass through untouched.
_ENV_REF = re.compile(r"\$(\$?)\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` placeholders in every string of a parsed YAML tree.

    Substitution happens *after* parsing, on string values only, so an environment variable can
    never inject YAML structure (a key, a list, an anchor) — it can only ever be a scalar.
    """
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str) or "${" not in value:
        return value

    def substitute(match: re.Match) -> str:
        escaped, name, operator, default = match.groups()
        if escaped:
            return match.group(0)[1:]  # "$${X}" -> literal "${X}"
        current = os.environ.get(name)
        if operator == ":-" and not current:
            return default or ""
        if operator == "-" and current is None:
            return default or ""
        if current is None:
            logger.warning("config references ${%s}, which is not set; using an empty string", name)
            return ""
        return current

    return _ENV_REF.sub(substitute, value)


def load_yaml(path: Path | str, *, expand: bool = True) -> dict[str, Any]:
    """Load a YAML mapping, expanding ``${ENV}`` placeholders unless ``expand`` is False.

    The SSH allow-list is loaded with ``expand=False``: it is the security boundary for node
    access, so nothing outside the reviewed file may alter it.
    """
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a YAML mapping at the top level")
    return expand_env(data) if expand else data


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
    # known_hosts file used to verify this director's host key. Empty falls back to
    # ssh.known_hosts in settings.yaml; if that is empty too, host keys are not verified.
    known_hosts: str = ""


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
    notify_timeout_seconds: float = 15
    # Env var holding a bearer token sent with the notification, for webhooks that need auth.
    notify_token_env: str = ""
    # Budget for the RCA prompt. The defaults fit an 8B model with a 4096-token context; raise
    # them when the report agent runs on a larger-context model.
    max_tokens: int = 900
    evidence_items: int = 6
    evidence_max_chars: int = 1500
    evidence_result_chars: int = 250


class SSHConfig(BaseModel):
    connect_timeout_seconds: float = 30
    # Upper bound for a single command on the director / overcloud hop.
    command_timeout_seconds: float = 120
    # 0 disables SSH keepalives.
    keepalive_interval_seconds: float = 0
    # Default known_hosts for every director (a director's own known_hosts wins). Empty
    # disables host-key verification, which is logged as a warning at every connection.
    known_hosts: str = ""
    # Command output is trimmed to this many characters before it reaches a prompt.
    output_max_chars: int = 1500


class EventsConfig(BaseModel):
    # In-memory ring buffer (and number of events replayed from the store on start).
    buffer_size: int = 4000
    # Per-SSE-client queue; a slow client loses its oldest events beyond this.
    subscriber_queue_size: int = 512
    # Events sent to a freshly connected dashboard before live streaming begins.
    stream_replay: int = 200


class OrchestratorConfig(BaseModel):
    # On shutdown, running investigations get this long to finish before being cancelled (they
    # resume from their checkpoint on the next start). Keep it below the pod's
    # terminationGracePeriodSeconds.
    shutdown_grace_seconds: float = 5
    # Re-run incidents left unfinished by a previous pod on startup.
    resume_on_start: bool = True


class DemoConfig(BaseModel):
    # POST /api/demo writes a scripted incident into the real case store. Turn it off in
    # production so the dashboard only ever shows genuine incidents.
    enabled: bool = True
    step_pause_seconds: float = 1.1


class ApiConfig(BaseModel):
    # How much the dashboard's /api/overview call returns.
    overview_incidents: int = 100
    overview_alarms: int = 200


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Feeds config/settings.yaml into AppConfig below env vars in precedence."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = get_settings().config_dir / "settings.yaml"
        if not path.exists():
            return {}
        return load_yaml(path)


class AppConfig(BaseSettings):
    """Runtime tunables. Env wins over settings.yaml: TSAGENT_CORRELATION__WINDOW_SECONDS=60."""

    model_config = SettingsConfigDict(
        env_prefix="TSAGENT_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    investigation: InvestigationConfig = Field(default_factory=InvestigationConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    demo: DemoConfig = Field(default_factory=DemoConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

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
    """Process-level settings, from TSAGENT_* env vars (or a local .env file).

    These decide where things live and how the process is served, so they are needed before any
    config file is read — which is why they are env-only rather than part of settings.yaml.
    """

    model_config = SettingsConfigDict(env_prefix="TSAGENT_", env_file=".env", extra="ignore")

    # -- paths --
    config_dir: Path = Path("config")
    db_path: Path = Path("data/cases.db")
    checkpoint_path: Path = Path("data/checkpoints.db")
    # Built dashboard (web/dist). Empty disables serving the UI from the API process.
    ui_dir: str = ""

    # -- HTTP server --
    host: str = "::"  # dual-stack; use 0.0.0.0 where the pod has no IPv6
    port: int = 8080
    # Path prefix when the Route exposes the app under one (route.path). The router forwards the
    # prefix unchanged; requests with and without it are both served, so probes need no change.
    root_path: str = ""
    proxy_headers: bool = True  # trust X-Forwarded-* from the OpenShift router
    forwarded_allow_ips: str = "*"
    timeout_keep_alive: int = 5
    timeout_graceful_shutdown: int = 20
    # Serve HTTPS directly (e.g. an OpenShift service-serving certificate for a reencrypt
    # Route). Both empty means plain HTTP.
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    # Comma-separated. "*" is fine when the UI is served same-origin by this process.
    cors_origins: str = "*"
    metrics_enabled: bool = True

    # -- logging --
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"
    access_log: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]

    def load_inventory(self) -> ClusterInventory:
        path = self.config_dir / "clusters.yaml"
        if not path.exists():
            logger.warning("%s missing — no clusters configured", path)
            return ClusterInventory()
        return ClusterInventory.model_validate(load_yaml(path))

    def load_app_config(self) -> AppConfig:
        return AppConfig()


@lru_cache
def get_settings() -> Settings:
    return Settings()
