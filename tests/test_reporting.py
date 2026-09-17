"""Report rendering: structure, escaping and the empty-evidence cases."""

from __future__ import annotations

from tsagent.reporting import render_report
from tsagent.reporting.renderer import md_cell

INCIDENT = {
    "id": "inc-abc123",
    "cluster": "lab-cluster-1",
    "group_key": "lab-cluster-1|node=overcloud-compute-0",
    "created_at": "2026-08-06T10:00:00Z",
    "summary": "2 firing alarm(s) [critical] on overcloud-compute-0",
}
ALARMS = [
    {
        "name": "NodeDown", "severity": "critical", "node": "overcloud-compute-0",
        "starts_at": "2026-08-06T09:58:00Z", "received_at": "2026-08-06T09:58:05Z",
    }
]
RCA = {
    "root_cause": "The /var filesystem filled",
    "confidence": 0.82,
    "recommendations": ["Rotate logs in /var/log/containers"],
    "caveats": [],
    "timeline": ["Disk filled", "Exporter stopped"],
}


def render(**overrides):
    kwargs = {
        "incident": INCIDENT,
        "alarms": ALARMS,
        "plan": {"hypotheses": ["The /var filesystem filled"]},
        "promql_evidence": [
            {
                "goal": "Is the exporter up?",
                "request": 'up{job="node-exporter",site="lab"}',
                "result": "up{instance=overcloud-compute-0:9100} = 0",
                "ok": True,
            }
        ],
        "node_evidence": [
            {
                "goal": "check disk",
                "request": "overcloud-compute-0: df -h",
                "result": "/dev/vda1 50G 50G 0 100% /var",
                "ok": True,
            }
        ],
        "rca": RCA,
        "caveats": ["Reflection on the node step was skipped."],
        "decision_reason": "metrics were ambiguous",
    }
    kwargs.update(overrides)
    return render_report(**kwargs)


def test_report_contains_every_required_section():
    report = render()
    for heading in (
        "## Summary",
        "## Alarm timeline",
        "## Correlation rationale",
        "## Hypotheses considered",
        "## Evidence — metrics",
        "## Evidence — node investigation",
        "## Recommended corrective actions",
        "## Caveats",
    ):
        assert heading in report


def test_header_carries_the_root_cause_and_confidence():
    report = render()
    assert report.startswith("# Incident inc-abc123 — The /var filesystem filled")
    assert "| **Confidence** | 82% |" in report


def test_pipes_in_the_group_key_do_not_break_the_table():
    report = render()
    table_row = next(line for line in report.splitlines() if "Grouped by" in line)
    # Escaped, so the row still has exactly the two content columns the header declares.
    assert "lab-cluster-1\\|node=overcloud-compute-0" in table_row
    assert table_row.count("|") - table_row.count("\\|") == 3


def test_evidence_is_rendered_verbatim_in_code_blocks():
    report = render()
    assert '```promql\nup{job="node-exporter",site="lab"}\n```' in report
    assert "$ overcloud-compute-0: df -h" in report


def test_recommendations_and_timeline_are_numbered():
    report = render()
    assert "1. Rotate logs in /var/log/containers" in report
    assert "1. Disk filled" in report


def test_no_node_evidence_explains_why():
    report = render(node_evidence=[])
    assert "Node access was not required: metrics were ambiguous" in report


def test_no_evidence_at_all_still_renders():
    report = render(promql_evidence=[], node_evidence=[], caveats=[], plan={"hypotheses": []})
    assert "_No metric evidence was collected._" in report
    assert "## Caveats" not in report


def test_failed_evidence_is_marked():
    report = render(
        promql_evidence=[
            {"goal": "bad query", "request": "up{", "result": "ERROR: parse error", "ok": False}
        ]
    )
    assert "**bad query** (failed)" in report


def test_missing_rca_fields_do_not_crash_the_template():
    report = render(rca={"root_cause": "", "confidence": 0.0, "recommendations": [], "timeline": []})
    assert "root cause undetermined" in report
    assert "_No specific action could be recommended" in report


def test_md_cell_escapes_pipes_and_newlines():
    assert md_cell("a|b") == "a\\|b"
    assert md_cell("a\nb") == "a b"
    assert md_cell(None) == ""
