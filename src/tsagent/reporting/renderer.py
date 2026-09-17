"""Markdown RCA rendering.

The template is deterministic: the LLM supplies the root cause, recommendations and timeline,
but the evidence tables are rendered from what was actually executed, so a report cannot claim
a query result that never happened.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"


def md_cell(value: object) -> str:
    """Escape a value for a markdown table cell.

    Group keys look like ``lab-cluster-1|node=compute-0``; an unescaped pipe silently splits the
    row into extra columns and the table renders as nonsense.
    """
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


@lru_cache
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(default=False, default_for_string=False),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["md_cell"] = md_cell
    return env


def render_report(
    incident: dict[str, Any],
    alarms: list[dict[str, Any]],
    plan: dict[str, Any],
    promql_evidence: list[dict[str, Any]],
    node_evidence: list[dict[str, Any]],
    rca: dict[str, Any],
    caveats: list[str],
    decision_reason: str = "",
) -> str:
    template = _environment().get_template("report.md.j2")
    return template.render(
        incident=incident,
        alarms=alarms,
        plan=plan or {"hypotheses": []},
        promql_evidence=promql_evidence or [],
        node_evidence=node_evidence or [],
        rca=rca,
        caveats=caveats or [],
        decision_reason=decision_reason,
    )
