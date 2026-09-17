"""Prometheus tool: cluster scoping, result summarization and the HTTP client."""

from __future__ import annotations

import httpx
import pytest
import respx

from tsagent.tools.prometheus import PrometheusClient, QueryResult, inject_label_matchers

MATCHERS = {"site": "lab"}


# -- label injection --------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("up", 'up{site="lab"}'),
        ('up{job="node-exporter"}', 'up{job="node-exporter",site="lab"}'),
        ("node_load5", 'node_load5{site="lab"}'),
    ],
)
def test_injects_matchers_into_selectors(expr, expected):
    assert inject_label_matchers(expr, MATCHERS) == expected


def test_leaves_function_names_alone():
    result = inject_label_matchers("rate(node_cpu_seconds_total[5m])", MATCHERS)
    assert result == 'rate(node_cpu_seconds_total{site="lab"}[5m])'
    assert "rate{" not in result


def test_does_not_duplicate_an_existing_matcher():
    expr = 'up{site="lab",job="x"}'
    assert inject_label_matchers(expr, MATCHERS) == expr


def test_skips_aggregation_operators_and_grouping_labels():
    expr = "sum by (instance) (node_load5)"
    result = inject_label_matchers(expr, MATCHERS)
    assert result == 'sum by (instance) (node_load5{site="lab"})'


def test_skips_label_names_inside_without_and_on():
    expr = "sum without (cpu) (rate(node_cpu_seconds_total[5m]))"
    result = inject_label_matchers(expr, MATCHERS)
    assert result == 'sum without (cpu) (rate(node_cpu_seconds_total{site="lab"}[5m]))'


def test_does_not_rewrite_inside_string_literals():
    expr = 'up{instance=~"compute.*"}'
    result = inject_label_matchers(expr, MATCHERS)
    assert result == 'up{instance=~"compute.*",site="lab"}'


def test_handles_a_realistic_multi_selector_expression():
    expr = (
        'node_filesystem_avail_bytes{mountpoint="/var"} / '
        'node_filesystem_size_bytes{mountpoint="/var"} < 0.1'
    )
    result = inject_label_matchers(expr, MATCHERS)
    assert result.count('site="lab"') == 2


def test_no_matchers_is_a_passthrough():
    assert inject_label_matchers("up", {}) == "up"


def test_scoping_a_shared_prometheus_uses_every_matcher():
    result = inject_label_matchers("up", {"site": "lab", "cluster": "c1"})
    assert 'cluster="c1"' in result and 'site="lab"' in result


# -- result summarization ---------------------------------------------------


def test_summarize_instant_vector():
    result = QueryResult(
        expr="up",
        ok=True,
        result_type="vector",
        series=[{"metric": {"__name__": "up", "instance": "compute-0:9100"}, "value": [1, "0"]}],
    )
    summary = result.summarize()
    assert "up{instance=compute-0:9100} = 0" in summary


def test_summarize_range_collapses_points():
    """A range response must not reach a prompt as hundreds of raw samples."""
    values = [[i, str(i * 1.5)] for i in range(200)]
    result = QueryResult(
        expr="node_load5",
        ok=True,
        result_type="matrix",
        series=[{"metric": {"__name__": "node_load5"}, "values": values}],
    )
    summary = result.summarize()
    assert "200 points" in summary and "min=0" in summary
    assert len(summary) < 200


def test_summarize_truncates_many_series():
    series = [{"metric": {"__name__": "up", "instance": f"n{i}"}, "value": [1, "1"]} for i in range(20)]
    summary = QueryResult(expr="up", ok=True, series=series).summarize(max_series=3)
    assert "and 17 more series" in summary


def test_summarize_reports_errors_and_emptiness():
    assert "ERROR: bad query" in QueryResult(expr="x", ok=False, error="bad query").summarize()
    assert "empty result" in QueryResult(expr="x", ok=True, series=[]).summarize()


# -- client -----------------------------------------------------------------


@respx.mock
async def test_query_parses_a_successful_response():
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"__name__": "up"}, "value": [1, "1"]}],
                },
            },
        )
    )
    result = await PrometheusClient("http://prom:9090").query("up")
    assert result.ok and len(result.series) == 1


@respx.mock
async def test_query_surfaces_a_promql_parse_error():
    """The 400 body carries the parse error — that is what the reflection step needs to see."""
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(
            400, json={"status": "error", "error": "parse error: unexpected end of input"}
        )
    )
    result = await PrometheusClient("http://prom:9090").query("up{")
    assert not result.ok
    assert "parse error" in result.error


@respx.mock
async def test_unreachable_prometheus_is_a_failed_result_not_an_exception():
    respx.get("http://prom:9090/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
    result = await PrometheusClient("http://prom:9090").query("up")
    assert not result.ok and "request failed" in result.error


@respx.mock
async def test_query_range_sends_a_time_window():
    route = respx.get("http://prom:9090/api/v1/query_range").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )
    )
    await PrometheusClient("http://prom:9090").query_range("up")
    params = route.calls[0].request.url.params
    assert "start" in params and "end" in params and params["step"] == "60s"


# -- TLS, auth and unreachability -------------------------------------------


def test_ca_bundle_is_passed_to_httpx_when_it_exists(tmp_path):
    ca = tmp_path / "service-ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----")
    client = PrometheusClient("https://prom:9090", ca_bundle=str(ca))
    assert client._client_kwargs()["verify"] == str(ca)


def test_missing_ca_bundle_falls_back_to_system_trust(tmp_path):
    client = PrometheusClient("https://prom:9090", ca_bundle=str(tmp_path / "nope.crt"))
    assert client._client_kwargs()["verify"] is True


def test_verify_can_be_disabled():
    assert PrometheusClient("https://prom:9090", verify_ssl=False)._client_kwargs()["verify"] is False


def test_bearer_token_becomes_an_auth_header():
    kwargs = PrometheusClient("https://prom:9090", bearer_token="abc123")._client_kwargs()
    assert kwargs["headers"]["Authorization"] == "Bearer abc123"


def test_no_token_sends_no_auth_header():
    assert "headers" not in PrometheusClient("http://prom:9090")._client_kwargs()


def test_from_config_reads_a_token_file(tmp_path):
    from tsagent.config import PrometheusInstance

    token = tmp_path / "token"
    token.write_text("  sa-token-value\n")
    client = PrometheusClient.from_config(
        PrometheusInstance(url="https://prom:9090", bearer_token_file=str(token))
    )
    assert client.bearer_token == "sa-token-value"


def test_from_config_prefers_the_env_var(tmp_path, monkeypatch):
    from tsagent.config import PrometheusInstance

    monkeypatch.setenv("PROM_TOKEN", "from-env")
    token = tmp_path / "token"
    token.write_text("from-file")
    client = PrometheusClient.from_config(
        PrometheusInstance(
            url="https://prom:9090", bearer_token_env="PROM_TOKEN", bearer_token_file=str(token)
        )
    )
    assert client.bearer_token == "from-env"


@respx.mock
async def test_a_tls_failure_is_marked_unreachable():
    """A TLS error is an infrastructure fault, not a bad query — the graph must tell them apart."""
    respx.get("https://prom:9090/api/v1/query").mock(
        side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate")
    )
    result = await PrometheusClient("https://prom:9090").query("up")
    assert not result.ok and result.unreachable
    assert "CERTIFICATE_VERIFY_FAILED" in result.error


@respx.mock
async def test_auth_failure_is_marked_unreachable():
    respx.get("https://prom:9090/api/v1/query").mock(return_value=httpx.Response(403))
    result = await PrometheusClient("https://prom:9090").query("up")
    assert not result.ok and result.unreachable
    assert "bearer token" in result.error


@respx.mock
async def test_a_rejected_query_is_not_marked_unreachable():
    """A PromQL syntax error IS worth retrying with a better query, so it must not be conflated."""
    respx.get("http://prom:9090/api/v1/query").mock(
        return_value=httpx.Response(400, json={"status": "error", "error": "parse error"})
    )
    result = await PrometheusClient("http://prom:9090").query("up{")
    assert not result.ok and not result.unreachable
