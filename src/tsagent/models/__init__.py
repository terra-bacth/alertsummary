"""Domain models: AlertManager payloads, alarms, incidents, evidence, reports."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# AlertManager webhook payload (version "4")
# ---------------------------------------------------------------------------


class AMAlert(BaseModel):
    """One alert inside an AlertManager webhook POST.

    Accepts both shapes seen in the wild: the classic webhook v4 payload, where ``status`` is the
    string ``"firing"``/``"resolved"``, and the Alertmanager API v2 alert shape that Service
    Telemetry Framework forwards, where ``status`` is an object carrying ``state``,
    ``silencedBy`` and ``inhibitedBy``.
    """

    status: str = "firing"  # firing | resolved
    suppressed: bool = False  # silenced or inhibited upstream
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    generatorURL: str = ""
    fingerprint: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_api_v2_status(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        status = data.get("status")
        if isinstance(status, dict):
            data = dict(data)
            state = str(status.get("state") or "").lower()
            # API v2 states are active | suppressed | unprocessed; only a resolved alert is
            # anything other than firing as far as investigation is concerned.
            data["status"] = "resolved" if state == "resolved" else "firing"
            data["suppressed"] = bool(status.get("silencedBy") or status.get("inhibitedBy"))
        return data

    def computed_fingerprint(self) -> str:
        if self.fingerprint:
            return self.fingerprint
        raw = "|".join(f"{k}={v}" for k, v in sorted(self.labels.items()))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def rule_expr(self) -> str:
        """The alerting rule's own PromQL, recovered from generatorURL.

        Prometheus encodes the firing expression into that link. It names the exact metric the
        rule watches, which is far better grounding for the PromQL agent than an alert name — and
        it costs nothing to extract.
        """
        if not self.generatorURL:
            return ""
        try:
            query = parse_qs(urlparse(self.generatorURL).query)
        except ValueError:
            return ""
        for key in ("g0.expr", "query", "expr"):
            values = query.get(key)
            if values and values[0].strip():
                return values[0].strip()
        return ""


class AMWebhook(BaseModel):
    """AlertManager webhook envelope (payload version 4)."""

    version: str = "4"
    groupKey: str = ""
    status: str = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[AMAlert] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal domain models
# ---------------------------------------------------------------------------


# Label names that carry the host, most specific first. Exporters and alerting rules are far from
# consistent about this: node_exporter uses `instance`, many RHOSP rules use `host` or `hostname`,
# and a rule that aggregates away `instance` often re-attaches it as `exported_instance`. Without
# a node the agent cannot escalate to SSH at all, so it is worth casting a wide net.
NODE_LABELS = (
    "instance",
    "node",
    "host",
    "hostname",
    "nodename",
    "fqdn",
    "server",
    "exported_instance",
)


def extract_node(labels: dict[str, str]) -> str:
    """Best-effort host name from alarm labels, with any port stripped."""
    for label in NODE_LABELS:
        value = (labels.get(label) or "").strip()
        if not value:
            continue
        # "compute-0:9100" -> "compute-0"; IPv6 literals keep their brackets intact.
        if value.startswith("["):
            host = value.split("]")[0].lstrip("[")
        else:
            host = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
        if host:
            return host
    return ""


class Alarm(BaseModel):
    """Normalized alarm stored by the ingest layer."""

    id: str = Field(default_factory=lambda: new_id("alm"))
    fingerprint: str
    status: str  # firing | resolved
    name: str  # alertname label
    severity: str = "unknown"
    node: str = ""  # instance/node label, port stripped
    site: str = ""  # site/cluster label if present
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    received_at: datetime = Field(default_factory=utcnow)
    incident_id: str | None = None
    suppressed: bool = False  # silenced or inhibited in AlertManager
    rule_expr: str = ""  # the alerting rule's own PromQL, from generatorURL

    @classmethod
    def from_am_alert(cls, a: AMAlert) -> Alarm:
        labels = a.labels
        node = extract_node(labels)
        return cls(
            fingerprint=a.computed_fingerprint(),
            status=a.status,
            name=labels.get("alertname", "unknown"),
            severity=labels.get("severity", "unknown"),
            node=node,
            site=labels.get("site", labels.get("cluster", "")),
            labels=labels,
            annotations=a.annotations,
            starts_at=a.startsAt,
            ends_at=a.endsAt,
            suppressed=a.suppressed,
            rule_expr=a.rule_expr(),
        )


class IncidentStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    REPORTED = "reported"
    FAILED = "failed"


class Incident(BaseModel):
    """A correlated group of alarms under investigation."""

    id: str = Field(default_factory=lambda: new_id("inc"))
    status: IncidentStatus = IncidentStatus.OPEN
    cluster: str = ""  # resolved cluster key from clusters.yaml
    group_key: str = ""  # what the correlation grouped on, e.g. node=... / site=...
    alarm_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    summary: str = ""


class EvidenceKind(StrEnum):
    PROMQL = "promql"
    NODE_CMD = "node_cmd"
    DIRECTOR_CMD = "director_cmd"
    NOTE = "note"


class Evidence(BaseModel):
    """One piece of collected evidence during investigation."""

    id: str = Field(default_factory=lambda: new_id("evd"))
    incident_id: str
    kind: EvidenceKind
    source: str = ""  # prometheus instance / node hostname / director
    request: str = ""  # the query or command executed
    result: str = ""  # (summarized) result
    ok: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Report(BaseModel):
    """Final RCA report for an incident."""

    id: str = Field(default_factory=lambda: new_id("rpt"))
    incident_id: str
    root_cause: str = ""
    confidence: float = 0.0  # 0..1
    recommendations: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    markdown: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
