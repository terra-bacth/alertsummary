"""Node agent: an iterative, allow-listed look at the affected nodes over the director hop.

The loop is: propose one command -> validate against the allow-list -> execute -> summarize ->
feed the summary back. The model chooses *which* allow-listed command to run; it never composes
the command that reaches a shell.
"""

from __future__ import annotations

import logging

from tsagent.graph.context import evidence_digest, incident_header
from tsagent.graph.deps import GraphDeps
from tsagent.graph.schemas import NodeCommand
from tsagent.graph.state import IncidentState
from tsagent.llm import LLMError
from tsagent.models import Evidence, EvidenceKind
from tsagent.tools.ssh import CommandNotAllowed

logger = logging.getLogger(__name__)

SYSTEM = (
    "You investigate an OpenStack node over SSH. You may only run read-only commands that match "
    "the allow-list you are given, exactly as written there. Run one command at a time, choose "
    "the one that best narrows the cause, and set done=true as soon as you can explain the "
    "alarms or no allowed command would add anything."
)


async def node_investigate_node(state: IncidentState, deps: GraphDeps) -> dict:
    cluster = state.get("cluster") or ""
    executor = deps.ssh_for(cluster)
    if executor is None:
        return {"caveats": [f"No director is configured for cluster {cluster!r}; SSH skipped."]}

    targets = state.get("target_nodes") or []
    if not targets:
        return {"caveats": ["Node investigation was requested but no target node was identified."]}

    node = targets[0]
    max_commands = deps.app_config.investigation.max_node_commands
    allowlist_text = deps.allowlist.describe("overcloud")
    # Which RHOSP release this is decides the whole command family. Told nothing, a model assumes
    # 16 and proposes systemctl for pacemaker bundles, then repeats it until the budget is gone.
    platform = await executor.platform_notes() if hasattr(executor, "platform_notes") else ""
    collected: list[dict] = []

    refused: list[str] = []
    ran_commands: set[str] = set()  # commands already executed successfully — no repeats

    for step in range(max_commands):
        parts = [incident_header(state), ""]
        if platform:
            parts.extend([platform, ""])
        parts.extend(
            [
                f"Target node: {node}",
                f"Metric evidence:\n{evidence_digest(state.get('promql_evidence') or [])}",
                "",
                f"Commands run so far:\n{evidence_digest(collected) if collected else '(none)'}",
                "",
            ]
        )
        if refused:
            # Spelling out the rejects stops the model re-proposing the same refused string,
            # which it did thirty times in one lab incident while insisting it was allowed.
            parts.extend(
                [
                    "Already REFUSED — do not propose these again:",
                    "\n".join(f"- {c}" for c in refused),
                    "",
                ]
            )
        parts.extend(
            [
                f"Allowed command patterns (regex):\n{allowlist_text}",
                "",
                f"You have {max_commands - step} command(s) left.",
                (
                    'Reply with JSON only: {"node": "...", "command": "...", '
                    '"rationale": "...", "done": false}'
                ),
            ]
        )
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ]

        try:
            proposal = await deps.gateway.complete_json("node_agent", messages, NodeCommand)
        except LLMError as exc:
            logger.error("node agent could not get a command: %s", exc)
            return {
                "node_evidence": collected,
                "caveats": [f"Node investigation stopped early ({exc})."],
            }

        if proposal.done or not proposal.command.strip():
            logger.info("node agent finished after %d command(s)", len(collected))
            break

        target = proposal.node.strip() or node
        if target not in targets:
            # Keep the agent inside the incident's blast radius even if it names another host.
            logger.warning("node agent proposed off-target node %r, redirecting to %s", target, node)
            target = node

        # A unit named *-podman-N is a pacemaker bundle, not a systemd unit: systemctl will
        # answer "could not be found" or "inactive", which is noise that costs a command and
        # prompts the reflector to retry. Redirect to pcs right away instead of burning the
        # budget and the model's attention on the wrong tool.
        import re as _re

        bundle = _re.search(r"tripleo_[A-Za-z0-9._-]+-podman-[0-9]+", proposal.command)
        if bundle and "systemctl" in proposal.command:
            hint = (
                f"REFUSED: {proposal.command} targets a pacemaker bundle "
                f"({bundle.group(0)}). -podman-N units are NOT systemd units. "
                "Use `sudo pcs resource status` or `sudo pcs status` instead."
            )
            logger.warning("redirecting systemctl-on-bundle to pcs: %s", proposal.command)
            record = {
                "kind": "node_cmd",
                "goal": proposal.rationale,
                "request": f"{target}: {proposal.command}",
                "result": hint,
                "ok": False,
            }
            collected.append(record)
            if deps.repo:
                await deps.repo.save_evidence(
                    Evidence(
                        incident_id=state["incident_id"],
                        kind=EvidenceKind.NODE_CMD,
                        source=target,
                        request=proposal.command,
                        result=hint,
                        ok=False,
                    )
                )
            continue

        if proposal.command in ran_commands:
            # The exact command already ran and produced a result. Re-running it is not
            # evidence — the 8B model does this when it is unsure (e.g. repeating
            # `cat /proc/meminfo` a dozen times). Record it as a note and push the model
            # to choose a different, narrower command instead of burning the budget.
            logger.warning("node agent repeated an already-run command: %s", proposal.command)
            record = {
                "kind": "node_cmd",
                "goal": proposal.rationale,
                "request": f"{target}: {proposal.command}",
                "result": "NOTE: this exact command already ran above with the same result. "
                          "Pick a DIFFERENT command that narrows the cause further.",
                "ok": False,
            }
            collected.append(record)
            if deps.repo:
                await deps.repo.save_evidence(
                    Evidence(
                        incident_id=state["incident_id"],
                        kind=EvidenceKind.NODE_CMD,
                        source=target,
                        request=proposal.command,
                        result=record["result"],
                        ok=False,
                    )
                )
            continue

        try:
            result = await executor.run_node(target, proposal.command)
            record = {
                "kind": "node_cmd",
                "goal": proposal.rationale,
                "request": f"{target}: {proposal.command}",
                "result": result.summarize(),
                "ok": result.ok,
            }
            if result.ok:
                ran_commands.add(proposal.command)
        except CommandNotAllowed as exc:
            # Refusals are fed back as evidence so the model can pick an allowed command next.
            logger.warning("refused node command: %s", exc)
            if proposal.command in refused:
                # It has been told already. Repeating the same refusal is not evidence, and the
                # loop is plainly not converging.
                logger.error("node agent repeated a refused command; abandoning node access")
                return {
                    "node_evidence": collected,
                    "caveats": [
                        (
                            "Node investigation stopped: the agent kept proposing commands the "
                            f"allow-list refuses (last: {proposal.command!r}). Either the command "
                            "it needs is not permitted, or the platform guidance is wrong."
                        )
                    ],
                }
            refused.append(proposal.command)
            record = {
                "kind": "node_cmd",
                "goal": proposal.rationale,
                "request": f"{target}: {proposal.command}",
                "result": f"REFUSED: {exc}",
                "ok": False,
            }

        collected.append(record)
        if deps.repo:
            await deps.repo.save_evidence(
                Evidence(
                    incident_id=state["incident_id"],
                    kind=EvidenceKind.NODE_CMD,
                    source=target,
                    request=proposal.command,
                    result=record["result"],
                    ok=record["ok"],
                )
            )
    else:
        logger.info("node agent hit the %d command budget", max_commands)
        return {
            "node_evidence": collected,
            "caveats": [f"Node investigation stopped at the {max_commands}-command budget."],
        }

    return {"node_evidence": collected}
