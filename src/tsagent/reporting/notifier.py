"""Optional outbound notification when an incident report is ready.

Delivery is best-effort on purpose: the report is already durable in the case store, so a
notification failure is a logged warning, never a failed investigation.
"""

from __future__ import annotations

import logging
import os

import httpx

from tsagent.models import Report

logger = logging.getLogger(__name__)


async def notify_report(
    webhook_url: str,
    report: Report,
    timeout_seconds: float = 15,
    token_env: str = "",
) -> bool:
    """POST a compact report summary to ``webhook_url``. Returns whether it was delivered."""
    if not webhook_url:
        return False

    payload = {
        "incident_id": report.incident_id,
        "root_cause": report.root_cause,
        "confidence": report.confidence,
        "recommendations": report.recommendations,
        "caveats": report.caveats,
        "markdown": report.markdown,
    }
    headers: dict[str, str] = {}
    token = os.environ.get(token_env, "") if token_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(webhook_url, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.warning(
                "report notification rejected: %s returned %s", webhook_url, response.status_code
            )
            return False
    except httpx.HTTPError as exc:
        logger.warning("report notification to %s failed: %s", webhook_url, exc)
        return False

    logger.info("report for %s delivered to %s", report.incident_id, webhook_url)
    return True
