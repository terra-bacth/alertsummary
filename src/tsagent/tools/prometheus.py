"""Prometheus query tool.

Results are summarized before they ever reach a prompt — CPU-hosted 8B models have a small
practical context budget, and a raw range response is thousands of tokens of noise.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from tsagent.models import utcnow

logger = logging.getLogger(__name__)

# Identifiers that are PromQL syntax rather than metric names, so they never take label matchers.
_RESERVED = {
    # binary and set operators
    "and", "or", "unless", "bool", "atan2",
    # modifiers
    "by", "without", "on", "ignoring", "group_left", "group_right", "offset", "start", "end",
    # aggregation operators — these are followed by "by"/"without" rather than "(", so the
    # function-call check below does not catch them
    "sum", "min", "max", "avg", "group", "stddev", "stdvar", "count", "count_values",
    "bottomk", "topk", "quantile", "limitk", "limit_ratio",
    # literals
    "inf", "nan",
}

# metric_name  or  metric_name{...}   — captures name and any existing matcher block.
_SELECTOR = re.compile(r"(?<![\w.:])([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^{}]*\})?")

# Regions whose identifiers are label names or text, never metric names.
_LABEL_LIST = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^()]*)\)")
_STRING = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")


def metric_names_in(expr: str) -> list[str]:
    """Extract the metric names a PromQL expression selects.

    Used to check a generated query against the catalogue of metrics that actually exist, before
    it is executed. Same skip rules as the label injector: functions, aggregation operators,
    grouping label lists and string literals are not metric names.
    """
    protected = _protected_spans(expr)
    names: list[str] = []
    for match in _SELECTOR.finditer(expr):
        name, existing = match.group(1), match.group(2)
        if name in _RESERVED or name in _FUNCTIONS:
            continue
        if any(lo <= match.start(1) < hi for lo, hi in protected):
            continue
        if existing is None and expr[match.end() :].lstrip().startswith("("):
            continue
        if name not in names:
            names.append(name)
    return names


# PromQL functions take a "(" immediately, so the call check catches them, but listing the common
# ones makes metric extraction robust against odd spacing.
_FUNCTIONS = {
    "rate", "irate", "increase", "delta", "idelta", "deriv", "predict_linear",
    "avg_over_time", "min_over_time", "max_over_time", "sum_over_time", "count_over_time",
    "last_over_time", "stddev_over_time", "quantile_over_time", "absent", "absent_over_time",
    "abs", "ceil", "floor", "round", "clamp", "clamp_max", "clamp_min", "exp", "ln", "log2",
    "log10", "sqrt", "sgn", "histogram_quantile", "label_replace", "label_join", "time",
    "timestamp", "vector", "scalar", "sort", "sort_desc", "changes", "resets", "day_of_week",
}


def _protected_spans(expr: str) -> list[tuple[int, int]]:
    """Character ranges the rewrite must not touch: grouping label lists and string literals."""
    spans = [m.span(1) for m in _LABEL_LIST.finditer(expr)]
    spans.extend(m.span(1) for m in _STRING.finditer(expr))
    return spans


def inject_label_matchers(expr: str, matchers: dict[str, str]) -> str:
    """Scope every metric selector in ``expr`` with ``matchers``.

    One Prometheus can serve several RHOSP clusters, so an unscoped query silently mixes metrics
    from clusters the incident has nothing to do with. Function calls, aggregation operators,
    grouping label lists and string literals are left alone; a selector that already constrains a
    label keeps its own value for that label.

    This is a textual rewrite, not a parse. It handles the selector shapes the PromQL agent is
    prompted to produce; exotic nesting is out of scope, which is acceptable because an
    over-scoped query fails loudly at Prometheus rather than returning another cluster's data.
    """
    if not matchers:
        return expr

    addition = ",".join(f'{k}="{v}"' for k, v in sorted(matchers.items()))
    protected = _protected_spans(expr)

    def replace(match: re.Match) -> str:
        name, existing = match.group(1), match.group(2)
        if name in _RESERVED:
            return match.group(0)
        start = match.start(1)
        if any(lo <= start < hi for lo, hi in protected):
            return match.group(0)
        # An identifier immediately followed by "(" is a function call, e.g. rate(...).
        if existing is None and expr[match.end() :].lstrip().startswith("("):
            return match.group(0)
        if existing is None:
            return f"{name}{{{addition}}}"
        inner = existing[1:-1].strip()
        if not inner:
            return f"{name}{{{addition}}}"
        missing = [
            f'{k}="{v}"'
            for k, v in sorted(matchers.items())
            if not re.search(rf"\b{k}\s*[=!~]", inner)
        ]
        if not missing:
            return match.group(0)
        return f"{name}{{{inner},{','.join(missing)}}}"

    return _SELECTOR.sub(replace, expr)


@dataclass
class QueryResult:
    expr: str
    ok: bool
    result_type: str = ""
    series: list[dict] = field(default_factory=list)
    error: str = ""
    # True when the request never reached Prometheus (DNS, TLS, refused connection). Distinct
    # from a rejected query: no amount of rewriting the PromQL will fix it, so the reflection
    # loop must not spend its retry budget re-asking the model.
    unreachable: bool = False

    def summarize(self, max_series: int = 8) -> str:
        """Compact, prompt-safe rendering of the result."""
        if not self.ok:
            return f"ERROR: {self.error}"
        if not self.series:
            return "empty result (no series matched)"
        lines: list[str] = []
        for s in self.series[:max_series]:
            labels = s.get("metric", {})
            label_str = ", ".join(
                f"{k}={v}" for k, v in sorted(labels.items()) if k != "__name__"
            )
            name = labels.get("__name__", "")
            if "value" in s:
                value = s["value"][1]
                lines.append(f"{name}{{{label_str}}} = {value}")
            else:
                values = [float(v[1]) for v in s.get("values", []) if _is_number(v[1])]
                if values:
                    lines.append(
                        f"{name}{{{label_str}}}: min={min(values):.4g} max={max(values):.4g} "
                        f"last={values[-1]:.4g} ({len(values)} points)"
                    )
                else:
                    lines.append(f"{name}{{{label_str}}}: no numeric points")
        if len(self.series) > max_series:
            lines.append(f"... and {len(self.series) - max_series} more series")
        return "\n".join(lines)


def _is_number(v: object) -> bool:
    try:
        float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


class PrometheusClient:
    """Thin async Prometheus HTTP API client."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 30,
        ca_bundle: str = "",
        verify_ssl: bool = True,
        bearer_token: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ca_bundle = ca_bundle
        self.verify_ssl = verify_ssl
        self.bearer_token = bearer_token
        if not verify_ssl:
            logger.warning(
                "TLS verification is disabled for %s — acceptable in a lab, not in production",
                self.base_url,
            )

    @classmethod
    def from_config(cls, instance) -> PrometheusClient:
        """Build from a PrometheusInstance, resolving the token from env or file."""
        token = ""
        if instance.bearer_token_env:
            token = os.environ.get(instance.bearer_token_env, "")
        if not token and instance.bearer_token_file:
            try:
                token = Path(instance.bearer_token_file).read_text().strip()
            except OSError as exc:
                logger.warning("could not read bearer token %s: %s", instance.bearer_token_file, exc)
        return cls(
            base_url=instance.url,
            timeout_seconds=instance.timeout_seconds,
            ca_bundle=instance.ca_bundle,
            verify_ssl=instance.verify_ssl,
            bearer_token=token,
        )

    def _client_kwargs(self) -> dict:
        # httpx takes either a bool or a CA bundle path for `verify`.
        verify: object = self.verify_ssl
        if self.verify_ssl and self.ca_bundle:
            if Path(self.ca_bundle).exists():
                verify = self.ca_bundle
            else:
                logger.warning("ca_bundle %s does not exist; using system trust", self.ca_bundle)
        kwargs: dict = {"timeout": self.timeout_seconds, "verify": verify}
        if self.bearer_token:
            kwargs["headers"] = {"Authorization": f"Bearer {self.bearer_token}"}
        return kwargs

    async def query(self, expr: str, at: datetime | None = None) -> QueryResult:
        params: dict[str, str] = {"query": expr}
        if at:
            params["time"] = str(at.timestamp())
        return await self._get("/api/v1/query", params, expr)

    async def query_range(
        self,
        expr: str,
        start: datetime | None = None,
        end: datetime | None = None,
        step: str = "60s",
    ) -> QueryResult:
        end = end or utcnow()
        start = start or (end - timedelta(hours=1))
        params = {
            "query": expr,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": step,
        }
        return await self._get("/api/v1/query_range", params, expr)

    async def check_syntax(self, expr: str) -> QueryResult:
        """Dry-run a query so a malformed expression is caught before it counts as evidence."""
        return await self.query(expr)

    async def _get(self, path: str, params: dict[str, str], expr: str) -> QueryResult:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("prometheus request failed: %s: %s", url, exc)
            return QueryResult(
                expr=expr, ok=False, error=f"request failed: {exc}", unreachable=True
            )

        if response.status_code in (401, 403):
            # Auth is a deployment problem, not a query problem — same class as a TLS failure.
            return QueryResult(
                expr=expr,
                ok=False,
                error=f"HTTP {response.status_code}: authentication failed (check bearer token)",
                unreachable=True,
            )

        if response.status_code >= 400:
            # Prometheus returns the parse error in the body for 400s — that is the useful part.
            detail = _error_detail(response) or response.text[:300]
            return QueryResult(expr=expr, ok=False, error=f"HTTP {response.status_code}: {detail}")

        payload = response.json()
        if payload.get("status") != "success":
            return QueryResult(expr=expr, ok=False, error=payload.get("error", "unknown error"))

        data = payload.get("data", {})
        return QueryResult(
            expr=expr,
            ok=True,
            result_type=data.get("resultType", ""),
            series=data.get("result", []),
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        return response.json().get("error", "")
    except ValueError:
        return ""
