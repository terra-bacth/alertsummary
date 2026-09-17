"""Per-alarm runbook hints fed to the planner.

An 8B model on CPU will not reliably invent good PromQL for a RHOSP-specific alarm from the
alarm name alone. A few lines of operator knowledge per alarm type costs almost nothing in
tokens and is the cheapest available quality lever — see the plan's risk 1.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import yaml

from tsagent.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def _load_runbooks() -> tuple[dict[str, str], tuple[str, ...]]:
    """Return (per-alarm hints, global hints).

    A ``global`` block describes the monitoring stack itself — which exporter is in use, where the
    host name lives — and applies whether or not a specific alarm matched.
    """
    directory = get_settings().config_dir / "runbooks"
    if not directory.is_dir():
        return {}, ()
    runbooks: dict[str, str] = {}
    globals_: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            logger.warning("skipping malformed runbook %s: %s", path, exc)
            continue
        for alarm_name, hint in (data.get("alarms") or {}).items():
            runbooks[alarm_name] = str(hint).strip()
        if data.get("global"):
            globals_.append(str(data["global"]).strip())
    logger.info(
        "loaded %d runbook hints and %d global hint(s) from %s",
        len(runbooks), len(globals_), directory,
    )
    return runbooks, tuple(globals_)


def runbook_hints(alarm_names: list[str]) -> str:
    """Return hint text for the alarms in an incident, or a neutral line when none match."""
    runbooks, globals_ = _load_runbooks()
    blocks = [
        f"Runbook — {name}:\n{runbooks[name]}" for name in alarm_names if name in runbooks
    ]
    if not blocks:
        blocks = [
            "No runbook entry exists for these alarms; rely on standard node/service metrics."
        ]
    blocks.extend(globals_)
    return "\n\n".join(blocks)


def clear_cache() -> None:
    """Drop the cached runbooks (tests and config reloads)."""
    _load_runbooks.cache_clear()
