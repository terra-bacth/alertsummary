"""Demo driver: a live, fully-scripted simulation of the tsagent pipeline.

Why this exists: the real pipeline is impressive but slow (minutes on CPU inference) and needs a
real AlertManager + Prometheus + LLM + director. For the dashboard to feel *alive* — and for you
to be able to show it off from a laptop merely pointed at a Route — this module replays a
cinematic, PLANNED incident sequence through the SAME event system the real pipeline uses.

Separation of concerns:

* The events streamed here are the same ``Event`` objects the graph nodes emit. The UI cannot
  tell the difference.
* State tables (alarms -> incidents -> evidence -> reports) are written through the real
  ``Repository``, so `/incidents`, `/alarms`, `/api/overview`, `/api/events` all light up.
* Confirmation evidence and the RCA are INSERTED by this driver. No LLM is inferred, and every
  command run is present in ``config/ssh_allowlist.yaml`` — the same allow-list the real node
  agent must obey.
"""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# A full, believable investigation that exercises every stage and every highlight colour:
# ingest -> correlate -> plan -> promql (reject + retry + execute) -> reflect -> decide ->
# node (refusal, redirect, real commands) -> reflect -> report.
#
# Note: the "result" strings in node evidence are ILLUSTRATIVE output for the demo screen.
# The driver is not running commands anywhere; it only writes what a successful run would have
# written, and every command text is allow-listed so the screen never shows an impossible action.
_SCRIPT: tuple[tuple[str, dict], ...] = (
    ("agent.started_plan", {
        "type": "agent.started", "status": "running", "agent": "planner", "step": "plan",
        "title": "Planner engaged — analysing 3 alarm signatures",
        "detail": "Reading runbook hints for hugepages / High Hugepage Usage / NodeFilesystemSpace.",
    }),
    ("plan.completed", {
        "type": "plan.completed", "status": "ok", "agent": "planner", "step": "plan",
        "title": "Investigation plan composed",
        "detail": "2 hypotheses ranked, 3 metric checks queued.",
        "data": {"plan": {
            "hypotheses": [
                "Guests pinned to a NUMA node that ran out of 1G hugepages",
                "An oversized guest is consuming hugepage + filesystem headroom",
            ],
            "checks": [
                {"goal": "Free 1G hugepages per host", "metric_hint": "collectd_hugepages_vmpage_number"},
                {"goal": "nova_compute container health", "metric_hint": "sensubility_container_health_status"},
                {"goal": "Filesystem headroom", "metric_hint": "collectd_df_df_complex"},
            ],
        }},
    }),
    ("agent.started_promql", {
        "type": "agent.started", "status": "running", "agent": "promql", "step": "promql",
        "title": "PromQL agent synthesising queries",
        "detail": "Grounding in the collectd_* namespace; adapting worked examples from the query library.",
    }),
    ("query.rejected", {
        "type": "query.rejected", "status": "warn", "agent": "promql", "step": "promql",
        "title": "Query rejected — range vector in an instant query",
        "detail": "Strip the [5m] selector: instant queries must not carry a range vector.",
        "data": {"goal": "Free 1G hugepages",
                 "promql": "100 * sum by (host)(collectd_hugepages_vmpage_number[5m])"},
    }),
    ("reflection.retry", {
        "type": "reflection.retry", "status": "retry", "agent": "reflection", "step": "reflect_promql",
        "title": "Reflector demands a retry of PromQL",
        "detail": "A range vector in an instant query cannot be answered. Regenerate without square brackets.",
        "data": {"step": "promql", "retry": 1, "max": 2},
    }),
    ("query.executed_huge", {
        "type": "query.executed", "status": "ok", "agent": "promql", "step": "promql",
        "title": "Query answered — free hugepages = 0",
        "detail": 'collectd_hugepages_vmpage_number{type_instance="free"}',
        "data": {"goal": "Free 1G hugepages",
                 "promql": 'collectd_hugepages_vmpage_number{type_instance="free"}',
                 "result": 'collectd_hugepages_vmpage_number{host="compute-0",type_instance="free"} = 0'},
    }),
    ("query.executed_health", {
        "type": "query.executed", "status": "error", "agent": "promql", "step": "promql",
        "title": "Query answered — nova_compute health = 0",
        "detail": 'min_over_time(sensubility_container_health_status{process="nova_compute"}[5m])',
        "data": {"goal": "nova_compute container health",
                 "promql": 'min_over_time(sensubility_container_health_status{process="nova_compute"}[5m])',
                 "result": 'sensubility_container_health_status{host="compute-0",process="nova_compute"} = 0'},
    }),
    ("query.executed_df", {
        "type": "query.executed", "status": "ok", "agent": "promql", "step": "promql",
        "title": "Query answered — filesystem 96% used",
        "detail": 'collectd_df_df_complex{type_instance="used"}',
        "data": {"goal": "Filesystem headroom",
                 "promql": 'collectd_df_df_complex{type_instance="used"}',
                 "result": 'collectd_df_df_complex{host="compute-0",type_instance="used"} = 96.2 (%)'},
    }),
    ("reflection.approved_promql", {
        "type": "reflection.approved", "status": "ok", "agent": "reflection", "step": "reflect_promql",
        "title": "PromQL evidence accepted",
        "detail": "The queries confirm the hugepage pool is exhausted and nova_compute is failing its health check.",
    }),
    ("decision.made", {
        "type": "decision.made", "status": "ok", "agent": "decision", "step": "decide",
        "title": "Decision — escalate to node access",
        "detail": "Metrics confirm the symptom (health_status=0, free hugepages=0) but not the cause.",
        "data": {"decision": "need_node_access", "target_nodes": ["compute-0"],
                 "reason": "health_status=0 confirms the service is down, not why"},
    }),
    ("agent.started_node", {
        "type": "agent.started", "status": "running", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "Node agent connecting via director hop",
        "detail": "Inventory resolved via metalsmith — compute-0 → ctlplane 192.168.24.10.",
    }),
    ("cmd.refused", {
        "type": "cmd.refused", "status": "refused", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "Command refused by allow-list",
        "detail": "Interactive session and shell operators are forbidden; only read-only allow-listed commands may run.",
        "data": {"command": "sudo podman exec -it nova_compute bash", "target": "compute-0"},
    }),
    ("cmd.refused_repeat", {
        "type": "cmd.refused", "status": "error", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "Repeated refusal — redirecting the agent",
        "detail": "The same refused command was proposed again; steering to allow-listed alternatives.",
        "data": {"command": "sudo podman exec -it nova_compute bash", "target": "compute-0"},
    }),
    ("cmd.redirected", {
        "type": "cmd.redirected", "status": "warn", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "Redirected — pacemaker bundle via systemctl",
        "detail": "tripleo_nova_compute-podman-0 is a pacemaker bundle, not a systemd unit — using pcs instead.",
        "data": {"command": "sudo systemctl status tripleo_nova_compute-podman-0", "target": "compute-0"},
    }),
    ("cmd.executed_df", {
        "type": "cmd.executed", "status": "ok", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "df -h — /var/lib/nova at 97%",
        "detail": "df -h",
        "data": {"command": "df -h", "target": "compute-0",
                 "result": "/dev/vda2  40G  38G  0  97% /var/lib/nova"},
    }),
    ("cmd.executed_free", {
        "type": "cmd.executed", "status": "ok", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "free -m — hugepages fully allocated",
        "detail": "free -m",
        "data": {"command": "free -m", "target": "compute-0",
                 "result": "Mem: 256000 total  HugePages_Total=128 HugePages_Free=0"},
    }),
    ("cmd.executed_pcs", {
        "type": "cmd.executed", "status": "ok", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "pcs status — nova_compute bundle failed",
        "detail": "sudo pcs resource status",
        "data": {"command": "sudo pcs resource status", "target": "compute-0",
                 "result": "nova-compute-bundle-podman-0 (ocf::pacemaker:remote): Failed on compute-0"},
    }),
    ("cmd.executed_journal", {
        "type": "cmd.executed", "status": "ok", "agent": "node_agent", "step": "node",
        "node": "compute-0",
        "title": "journalctl — libvirt hugepage allocation failed",
        "detail": "sudo journalctl -u tripleo_nova_compute --no-pager -n 50",
        "data": {"command": "sudo journalctl -u tripleo_nova_compute --no-pager -n 50",
                 "target": "compute-0",
                 "result": "libvirt: unable to allocate hugepage 2048kB: cannot allocate memory"},
    }),
    ("reflection.approved_node", {
        "type": "reflection.approved", "status": "ok", "agent": "reflection", "step": "reflect_node",
        "title": "Node evidence accepted",
        "detail": "Commands converge on a memory/hugepage exhaustion picture; enough evidence to write the RCA.",
    }),
    ("agent.started_report", {
        "type": "agent.started", "status": "running", "agent": "report", "step": "report",
        "title": "Report agent drafting the RCA",
        "detail": "Synthesising 7 pieces of evidence into the root-cause section.",
    }),
    ("report.ready", {
        "type": "report.ready", "status": "ok", "agent": "report", "step": "report",
        "title": "RCA ready",
        "detail": "A guest exhausted the 1G hugepage pool on compute-0, starving nova_compute of backing pages.",
        "data": {
            "root_cause": ("A guest on compute-0 consumed the entire 1G hugepage pool "
                           "(HugePages_Free=0), starving nova_compute of backing pages; the "
                           "health check accordingly reported 0."),
            "confidence": 0.87,
            "recommendations": [
                "Investigate the oversized/ballooning guest and cap its hugepage reservation.",
                "Add an earlier warning at <20% free hugepages and rebalance NUMA pinning.",
            ],
            "caveats": [],
        },
    }),
)


class DemoDriver:
    """Runs one scripted incident through the real event system and real data models."""

    def __init__(self, repo, events, inventory, step_pause: float = 1.1):
        self.repo = repo
        self.events = events
        self.inventory = inventory
        self.step_pause = step_pause
        self.scenario_id: str = ""
        self._task: asyncio.Task | None = None

    def cluster(self) -> str:
        clusters = list(self.inventory.clusters.keys())
        return clusters[0] if clusters else "lab-cluster-1"

    async def run(self) -> str:
        from tsagent.events import emit
        from tsagent.models import (
            Alarm,
            Evidence,
            EvidenceKind,
            Incident,
            IncidentStatus,
            Report,
        )
        from tsagent.reporting import render_report

        cluster = self.cluster()
        node = "compute-0"
        seed = f"{random.randint(1000, 9999)}"
        incident_id = f"inc-{seed}"
        self.scenario_id = incident_id

        # 1. Alarms arrive (persisted, then buffered by the real correlation engine)
        alarm_specs = [
            (
                "hugepages",
                "critical",
                "Huge pages free ratio is below the warning threshold",
                (
                    "100 * sum by (host)(collectd_hugepages_vmpage_number"
                    "{type_instance=\"free\"}) / sum by (host)"
                    "(collectd_hugepages_vmpage_number) < 10"
                ),
            ),
            (
                "High Hugepage Usage",
                "major",
                "Hugepage usage extremely high at 0% free",
                (
                    "100 * sum by (host)(collectd_hugepages_vmpage_number"
                    "{type_instance=\"free\"}) / sum by (host)"
                    "(collectd_hugepages_vmpage_number) < 5"
                ),
            ),
            (
                "NodeFilesystemSpace",
                "warning",
                "Filesystem /var/lib/nova usage going above 95%",
                (
                    "100 * collectd_df_df_complex{type_instance=\"used\"} "
                    "/ collectd_df_df_complex > 95"
                ),
            ),
        ]

        alarm_ids: list[str] = []
        for i, (name, severity, summary, expr) in enumerate(alarm_specs):
            alarm = Alarm(
                fingerprint=f"demo-{seed}-fp{i}",
                status="firing",
                name=name,
                severity=severity,
                node=node,
                site=cluster,
                labels={"alertname": name, "instance": f"{node}.lab:9100",
                        "site": "lab", "severity": severity},
                annotations={"summary": summary},
                rule_expr=expr,
            )
            await self.repo.save_alarm(alarm)
            alarm_ids.append(alarm.id)
            await emit(
                self.events, type="alarm.received", status="info",
                cluster=cluster, node=node, agent="system", step="ingest",
                title=f"{name} received",
                detail=f"{severity} — {summary}",
                data={"alarm": alarm.model_dump(mode="json")},
            )

        # Build the incident directly rather than going through the real correlation engine:
        # the real engine fires on_incident -> a real investigation (real LLM/SSH calls). The
        # demo is a scripted replay, so it must stay hermetic. We still buffer the alarms in a
        # way that mirrors the real grouping, but promotion is simulated.
        incident = Incident(
            id=incident_id, cluster=cluster,
            group_key=f"{cluster}|node={node}", alarm_ids=alarm_ids,
            summary=("3 firing alarm(s) [critical] on node compute-0: hugepages, "
                     "High Hugepage Usage, NodeFilesystemSpace"),
        )
        await self.repo.save_incident(incident)
        await emit(
            self.events, type="incident.opened", status="ok",
            incident_id=incident_id, cluster=cluster, agent="correlator", step="correlate",
            title="Incident opened",
            detail=incident.summary,
            data={"incident": incident.model_dump(mode="json")},
        )
        for alarm_id in alarm_ids:
            alarm = await self.repo.get_alarm(alarm_id)
            if alarm:
                alarm.incident_id = incident_id
                await self.repo.save_alarm(alarm)

        self.scenario_id = incident_id

        incident.status = IncidentStatus.INVESTIGATING
        await self.repo.save_incident(incident)
        await emit(
            self.events, type="incident.started", status="running",
            incident_id=incident_id, cluster=cluster, agent="orchestrator", step="investigate",
            title="Investigation started",
            detail=f"{incident.summary or incident.group_key} — 3 alarm(s).",
            data={"incident": incident.model_dump(mode="json")},
        )

        # 2. Replay the scripted timeline
        for _label, entry in _SCRIPT:
            await asyncio.sleep(self.step_pause)
            await emit(
                self.events,
                type=entry.get("type", "system"),
                status=entry.get("status", "info"),
                incident_id=incident_id,
                cluster=cluster,
                node=entry.get("node", ""),
                agent=entry.get("agent", ""),
                step=entry.get("step", ""),
                title=entry.get("title", ""),
                detail=entry.get("detail", ""),
                data=entry.get("data", {}),
            )

        # 3. Persist evidence + report through the real models and renderer.
        # Each evidence dict carries `goal` because the report template renders it — the same
        # shape the real promql_node / node_investigate_node build in their `record` dicts.
        evidence_defs = [
            (EvidenceKind.PROMQL, cluster, 'collectd_hugepages_vmpage_number{type_instance="free"}',
             "Free 1G hugepages per host", 'free = 0 for host="compute-0"', True),
            (EvidenceKind.PROMQL, cluster,
             'min_over_time(sensubility_container_health_status{process="nova_compute"}[5m])',
             "nova_compute container health", "nova_compute health_status = 0", True),
            (EvidenceKind.PROMQL, cluster, 'collectd_df_df_complex{type_instance="used"}',
             "Filesystem headroom", "/var/lib/nova used = 96.2%", True),
            (EvidenceKind.NODE_CMD, node, "df -h", "Confirm the full filesystem",
             "/dev/vda2  40G  38G  0  97% /var/lib/nova", True),
            (EvidenceKind.NODE_CMD, node, "free -m", "Confirm hugepage exhaustion",
             "HugePages_Total=128 HugePages_Free=0", True),
            (EvidenceKind.NODE_CMD, node, "sudo pcs resource status", "Confirm the failed bundle",
             "nova-compute-bundle-podman-0 (ocf::pacemaker:remote): Failed on compute-0", True),
            (EvidenceKind.NODE_CMD, node,
             "sudo journalctl -u tripleo_nova_compute --no-pager -n 50",
             "Find the allocation failure",
             "libvirt: unable to allocate hugepage 2048kB: cannot allocate memory", True),
        ]
        promql_dicts: list[dict] = []
        node_dicts: list[dict] = []
        for kind, source, request, goal, result, ok in evidence_defs:
            await self.repo.save_evidence(
                Evidence(incident_id=incident_id, kind=kind, source=source,
                         request=request, result=result, ok=ok)
            )
            record = {
                "kind": kind.value if isinstance(kind, EvidenceKind) else kind,
                "request": request,
                "result": result,
                "ok": ok,
                "goal": goal,
            }
            (promql_dicts if kind == EvidenceKind.PROMQL else node_dicts).append(record)

        plan = {
            "hypotheses": [
                "Guests pinned to a NUMA node that ran out of 1G hugepages",
                "An oversized guest is consuming hugepage + filesystem headroom",
            ],
            "checks": [
                {"goal": "Free 1G hugepages per host"},
                {"goal": "nova_compute container health"},
                {"goal": "Filesystem headroom"},
            ],
        }
        rca = {
            "root_cause": ("A guest on compute-0 consumed the entire 1G hugepage pool "
                           "(HugePages_Free=0), starving nova_compute of backing pages; the "
                           "health check accordingly reported 0."),
            "confidence": 0.87,
            "recommendations": [
                "Investigate the oversized/ballooning guest and cap its hugepage reservation.",
                "Add an earlier warning at <20% free hugepages and rebalance NUMA pinning.",
            ],
            "caveats": [],
            "timeline": [
                "Hugepage pool exhausted (free=0) on compute-0",
                "nova_compute cannot allocate backing pages",
                "sensubility health check → 0; bundle reported Failed",
            ],
        }
        markdown = render_report(
            incident=incident.model_dump(mode="json"),
            alarms=[(await self.repo.get_alarm(i)).model_dump(mode="json") for i in alarm_ids],
            plan=plan,
            promql_evidence=promql_dicts,
            node_evidence=node_dicts,
            rca=rca,
            caveats=[],
            decision_reason="health_status=0 confirms the symptom but not the cause",
        )
        report = Report(
            incident_id=incident_id,
            root_cause=rca["root_cause"],
            confidence=rca["confidence"],
            recommendations=rca["recommendations"],
            caveats=[],
            markdown=markdown,
            data={
                "plan": plan, "promql_evidence": [], "node_evidence": [],
                "reflections": [], "decision": "need_node_access",
                "decision_reason": "health_status=0 confirms the symptom but not the cause",
            },
        )
        await self.repo.save_report(report)

        stored = await self.repo.get_incident(incident_id)
        if stored:
            stored.status = IncidentStatus.REPORTED
            stored.summary = stored.summary or incident.summary
            await self.repo.save_incident(stored)

        await emit(
            self.events, type="incident.completed", status="ok",
            incident_id=incident_id, cluster=cluster, agent="orchestrator", step="report",
            title="Incident resolved",
            detail="RCA written, 2 recommendations issued.",
        )
        return incident_id

    def submit(self) -> asyncio.Task:
        """Run as a background task so the HTTP demo endpoint can return immediately."""
        self._task = asyncio.create_task(self.run())
        return self._task
