"""Node access over the RHOSP director hop, with a read-only command allow-list.

The agent pod never reaches an overcloud node directly:

    pod --SSH(troubleshoot)--> director --sudo -u stack--> ssh heat-admin@<ctlplane-ip> --> command

Validation happens *before* the command is composed into that chain. An LLM-proposed string that
does not match an anchored allow-list pattern never becomes part of a shell command at all.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import yaml

from tsagent.config import DirectorConfig, SSHConfig

logger = logging.getLogger(__name__)


class CommandNotAllowed(Exception):
    """A proposed command failed allow-list validation."""


_RELEASE_VERSION = re.compile(r"\b(\d+)\.\d+")

# What differs between the two releases, in the terms the node agent needs. Wrong guesses here
# are expensive: a model told nothing assumes RHOSP 16 and proposes systemctl for what is
# actually a pacemaker bundle, then repeats it until the command budget runs out.
PLATFORM_NOTES = {
    "16": (
        "This is RHOSP 16. Containerised services run under systemd units named tripleo_*, "
        "so `systemctl status tripleo_<service>` and `journalctl -u tripleo_<service>` are the "
        "way in. Pacemaker-managed services (galera, rabbitmq, haproxy, redis, "
        "openstack-cinder-volume) are bundle resources, NOT systemd units — inspect those with "
        "`sudo pcs status` and `sudo pcs resource status`. Containers are listed with "
        "`sudo podman ps`."
    ),
    "17": (
        "This is RHOSP 17. Containerised services run under systemd units named tripleo_*, "
        "so `systemctl status tripleo_<service>` and `journalctl -u tripleo_<service>` are the "
        "way in. Pacemaker-managed services (galera, rabbitmq, haproxy, redis, "
        "openstack-cinder-volume) are bundle resources, NOT systemd units — a name ending "
        "-podman-N is always a pacemaker bundle, so use `sudo pcs status` and "
        "`sudo pcs resource status` for those, never systemctl. Containers are listed with "
        "`sudo podman ps`."
    ),
}

_HOST_KEYS = ("Hostname", "hostname", "Node Name", "node_name", "Name", "name")
_ADDRESS_KEYS = ("IP Addresses", "ip_addresses", "Addresses", "addresses", "Networks", "networks")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# RFC 4291 IPv6 — matches the ::-compressed ctlplane addresses RHOSP 17 uses.
_IPV6 = re.compile(
    r"::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}"          # starts with ::
    r"|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"         # :: in the middle
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}[0-9a-fA-F]{1,4}"           # no :: (full form)
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"                           # ends with ::
    r"|::"                                                     # just ::
)
def _first_address(value: object) -> str:
    """Pull a ctlplane address out of whatever shape the tool reported.

    metalsmith gives ``"ctlplane=192.168.24.10"``, Nova gives
    ``{"ctlplane": ["192.168.24.10"]}``, and both have varied across releases — so this reads the
    text rather than trusting a schema, preferring the ctlplane network when it is named.
    """
    if isinstance(value, dict):
        for network, addresses in value.items():
            if "ctlplane" in str(network).lower():
                return _first_address(addresses)
        for addresses in value.values():
            found = _first_address(addresses)
            if found:
                return found
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_address(item)
            if found:
                return found
        return ""

    text = str(value or "")
    # "ctlplane=192.168.24.10, external=10.0.0.5" — prefer the ctlplane segment.
    for segment in text.split(","):
        if "ctlplane" in segment.lower():
            match = _IPV4.search(segment)
            if match:
                return match.group(0)
            match = _IPV6.search(segment)
            if match:
                return match.group(0)
    match = _IPV4.search(text)
    if match:
        return match.group(0)
    # findall + longest match catches ::-compressed addresses correctly
    matches6 = _IPV6.findall(text)
    if matches6:
        return max(matches6, key=len)
    return ""


def _parse_inventory(stdout: str) -> dict[str, str]:
    """Hostname -> ctlplane IP from a JSON node listing, whichever tool produced it.

    Handles three output shapes:
    - **List of dicts** — ``openstack server list -f json`` / Nova.
    - **Dict of dicts** — ``metalsmith -f json list``, where each key is a
      node hostname and the value contains ``hostname`` + ``ip_addresses``.
    - **Single dict** — ``metalsmith -f json show <node>``.
    """
    try:
        rows = json.loads(stdout or "[]")
    except ValueError as exc:
        logger.warning("could not parse overcloud inventory as JSON: %s", exc)
        return {}
    if isinstance(rows, dict):
        # Dict-of-dicts from metalsmith: iterate over the key/value pairs.
        node_items = rows.items()
    elif isinstance(rows, list):
        node_items = ((None, r) for r in rows)
    else:
        return {}

    inventory: dict[str, str] = {}
    for key, val in node_items:
        if not isinstance(val, dict):
            continue
        host = next((str(val[k]) for k in _HOST_KEYS if val.get(k)), "")
        if not host:
            host = str(val.get("hostname") or key or "")
        if not host:
            continue
        address = ""
        # Try standard address keys (IP Addresses, ip_addresses, etc.)
        for addr_key in _ADDRESS_KEYS:
            if addr_key in val:
                address = _first_address(val[addr_key])
                if address:
                    break
        # If still no address, try the ip_addresses network dict directly.
        # Metalsmith format: {"ctlplane": ["2405:200:5f02:2275::149"]}
        if not address:
            ip_data = val.get("ip_addresses")
            if isinstance(ip_data, dict):
                for net, addrs in ip_data.items():
                    if "ctlplane" in net.lower() and isinstance(addrs, list):
                        for a in addrs:
                            if isinstance(a, str) and (_IPV4.fullmatch(a) or _IPV6.fullmatch(a)):
                                address = a
                                break
                        if address:
                            break
        if address:
            inventory[host] = address
    return inventory


def _parse_host_result(stdout: str) -> str | None:
    """Extract an IPv4 from the output of ``host <fqdn>``.

    Example::

        ctrl03.rhosp2.jiocaas.com has address 192.168.24.10
    """
    if not stdout:
        return None
    for line in stdout.splitlines():
        match = _IPV4.search(line)
        if match:
            return match.group(0)
        matches6 = _IPV6.findall(line)
        if matches6:
            return max(matches6, key=len)
    return None


def _parse_getent_result(stdout: str) -> str | None:
    """Extract an IP from the output of ``getent hosts <fqdn>``.

    Example::

        192.168.24.10  ctrl03.rhosp2.jiocaas.com
        2405:200:5f02:2275::149  mhngprdc0204021422ctrl01
    """
    if not stdout:
        return None
    lines = stdout.splitlines()
    logger.info("_parse_getent_result: %d lines, first: %r",
                 len(lines), lines[0] if lines else "")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Fast path: first token is an IP.
        parts = line.split()
        if parts:
            if _IPV4.fullmatch(parts[0]):
                return parts[0]
            if _IPV6.fullmatch(parts[0]):
                return parts[0]
        # Fallback: scan entire line for any IP.
        match = _IPV4.search(line)
        if match:
            return match.group(0)
        matches6 = _IPV6.findall(line)
        if matches6:
            return max(matches6, key=len)
    logger.warning(
        "could not parse getent hosts output - no IP found. Raw: %r",
        stdout[:500],
    )
    return None


@dataclass
class CommandResult:
    command: str
    target: str  # "director" or the node name
    ok: bool
    exit_status: int = 0
    stdout: str = ""
    stderr: str = ""

    def summarize(self, max_chars: int = 1500) -> str:
        """Trim output before it reaches a prompt: keep the head and the tail, drop the middle."""
        if not self.ok:
            return f"ERROR: {self.stderr or 'command failed'}"
        text = self.stdout.strip() or "(no output)"
        if len(text) <= max_chars:
            return text
        head = text[: max_chars // 2]
        tail = text[-max_chars // 2 :]
        skipped = len(text) - max_chars
        return f"{head}\n... [{skipped} chars omitted] ...\n{tail}"


class AllowList:
    """Anchored regex allow-list, split by where the command runs."""

    def __init__(
        self,
        forbidden_characters: list[str],
        director: list[dict],
        overcloud: list[dict],
        forbidden_sequences: list[str] | None = None,
    ):
        self.forbidden_characters = forbidden_characters
        self.forbidden_sequences = forbidden_sequences or []
        self.director = [(re.compile(e["pattern"]), e.get("description", "")) for e in director]
        self.overcloud = [(re.compile(e["pattern"]), e.get("description", "")) for e in overcloud]

    @classmethod
    def load(cls, path: Path | str = Path("config/ssh_allowlist.yaml")) -> AllowList:
        path = Path(path)
        if not path.exists():
            # Fail closed: no allow-list means no node commands, not unrestricted access.
            logger.error("%s missing — node commands will all be refused", path)
            return cls([], [], [])
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(
            forbidden_characters=data.get("forbidden_characters") or [],
            director=data.get("director") or [],
            overcloud=data.get("overcloud") or [],
            forbidden_sequences=data.get("forbidden_sequences") or [],
        )

    def validate(self, command: str, scope: str = "overcloud") -> str:
        """Return the normalized command, or raise CommandNotAllowed."""
        candidate = (command or "").strip()
        if not candidate:
            raise CommandNotAllowed("empty command")
        for char in self.forbidden_characters:
            if char in candidate:
                raise CommandNotAllowed(f"command contains forbidden character {char!r}")
        for sequence in self.forbidden_sequences:
            if sequence in candidate:
                raise CommandNotAllowed(f"command contains forbidden sequence {sequence!r}")
        patterns = self.director if scope == "director" else self.overcloud
        for pattern, _ in patterns:
            if pattern.fullmatch(candidate):
                return candidate
        raise CommandNotAllowed(f"command not in {scope} allow-list: {candidate!r}")

    def describe(self, scope: str = "overcloud", limit: int = 40) -> str:
        """Render the allow-list for a prompt so the agent proposes commands that will pass."""
        patterns = self.director if scope == "director" else self.overcloud
        return "\n".join(f"- {p.pattern}  # {desc}" for p, desc in patterns[:limit])


class SSHExecutor:
    """Runs allow-listed commands on a cluster's director and its overcloud nodes."""

    def __init__(
        self,
        director: DirectorConfig,
        allowlist: AllowList,
        password: str | None = None,
        ssh_config: SSHConfig | None = None,
    ):
        self.director = director
        self.allowlist = allowlist
        self.password = password
        self.ssh_config = ssh_config or SSHConfig()
        self._inventory: dict[str, str] | None = None  # node name -> ctlplane ip
        self._version: str | None = None  # RHOSP major version, detected once

    # -- connection ---------------------------------------------------------

    async def _connect(self):
        import asyncssh

        cfg = self.ssh_config
        known_hosts = self.director.known_hosts or cfg.known_hosts
        if not known_hosts:
            logger.warning(
                "host key of director %s is not verified; set ssh.known_hosts (or the "
                "director's known_hosts) to a mounted known_hosts file", self.director.host,
            )
        kwargs: dict = {
            "host": self.director.host,
            "port": self.director.port,
            "username": self.director.user,
            "known_hosts": known_hosts or None,
            "connect_timeout": cfg.connect_timeout_seconds,
        }
        if cfg.keepalive_interval_seconds:
            kwargs["keepalive_interval"] = cfg.keepalive_interval_seconds
        if self.director.auth == "password":
            if not self.password:
                raise RuntimeError(
                    f"director {self.director.host} is configured for password auth but "
                    f"{self.director.password_env} is unset"
                )
            kwargs["password"] = self.password
        else:
            if not self.director.key_path:
                raise RuntimeError(f"director {self.director.host} has no key_path configured")
            kwargs["client_keys"] = [self.director.key_path]
        return await asyncssh.connect(**kwargs)

    async def _run_raw(self, command: str, target: str) -> CommandResult:
        try:
            async with await self._connect() as conn:
                result = await conn.run(
                    command, check=False, timeout=self.ssh_config.command_timeout_seconds
                )
        except Exception as exc:  # noqa: BLE001 - asyncssh raises a wide family of errors
            logger.warning("ssh to %s failed: %s", self.director.host, exc)
            return CommandResult(command=command, target=target, ok=False, stderr=str(exc))

        exit_status = result.exit_status or 0
        return CommandResult(
            command=command,
            target=target,
            ok=exit_status == 0,
            exit_status=exit_status,
            stdout=(result.stdout or "").strip(),
            stderr=(result.stderr or "").strip(),
        )

    # -- director -----------------------------------------------------------

    async def run_director(self, command: str) -> CommandResult:
        """Run an allow-listed command on the director as the stack user."""
        validated = self.allowlist.validate(command, scope="director")
        inner = validated
        if validated.split(" ", 1)[0] in ("openstack", "metalsmith"):
            # stackrc is sourced here rather than in the allow-list so patterns stay operator-free.
            inner = f"source ~/stackrc && {validated}"
        wrapped = f"sudo -u {shlex.quote(self.director.stack_user)} -i bash -c {shlex.quote(inner)}"
        logger.info("director command: %s", validated)
        return await self._run_raw(wrapped, target="director")

    # -- overcloud nodes ----------------------------------------------------

    # RHOSP 17 removed Nova from the undercloud — provisioning moved to metalsmith with ephemeral
    # Heat — so `openstack server list` there finds no compute endpoint in the catalogue and
    # fails with "public endpoint for compute service ... region not found".
    INVENTORY_COMMANDS: ClassVar[dict[str, tuple[str, ...]]] = {
        # metalsmith -f is a global option. Some build versions require it before "list".
        "17": (
            "metalsmith -f json list",
            "metalsmith list -f json",
            "metalsmith list --format json",
            "openstack baremetal node list -f json",
        ),
        "16": ("openstack server list -f json", "metalsmith list -f json"),
    }
    # Order when the version cannot be determined: newest-first across all tools.
    INVENTORY_FALLBACK: ClassVar[tuple[str, ...]] = (
        "metalsmith -f json list",
        "metalsmith list -f json",
        "metalsmith list --format json",
        "openstack baremetal node list -f json",
        "openstack server list -f json",
    )
    OVERCLOUD_USERS: ClassVar[dict[str, str]] = {"16": "heat-admin", "17": "tripleo-admin"}

    # -- platform -----------------------------------------------------------

    async def detect_version(self) -> str:
        """RHOSP major version from the director, cached. Empty when it cannot be determined."""
        if self._version is not None:
            return self._version
        if self.director.osp_version:
            self._version = self.director.osp_version.split(".")[0]
            logger.info("RHOSP version %s (from config)", self._version)
            return self._version

        result = await self.run_director("cat /etc/rhosp-release")
        version = ""
        if result.ok:
            match = _RELEASE_VERSION.search(result.stdout)
            if match:
                version = match.group(1)
                logger.info("RHOSP version %s detected: %s", version, result.stdout.strip()[:80])
        if not version:
            logger.warning(
                "could not determine the RHOSP version (%s); command selection falls back to "
                "probing. Set director.osp_version to remove the guesswork.",
                (result.stderr or "no version in /etc/rhosp-release")[:120],
            )
        self._version = version
        return version

    async def platform_notes(self) -> str:
        """Version-specific guidance for the node agent's prompt."""
        version = await self.detect_version()
        return PLATFORM_NOTES.get(version, "")

    async def overcloud_user(self) -> str:
        """The SSH account on overcloud nodes, from config or the detected version."""
        if self.director.overcloud_user:
            return self.director.overcloud_user
        version = await self.detect_version()
        # Unknown falls back to heat-admin rather than tripleo-admin: guessing wrong on a
        # RHOSP 16 estate would break something that previously worked.
        return self.OVERCLOUD_USERS.get(version, "heat-admin")

    async def list_nodes(self, refresh: bool = False) -> dict[str, str]:
        """Map overcloud hostnames to ctlplane IPs, via the director."""
        if self._inventory is not None and not refresh:
            return self._inventory

        # A statically configured map wins outright: it needs no undercloud call and is the
        # escape hatch when neither tool reports the hostnames an operator's alarms use.
        inventory: dict[str, str] = dict(self.director.nodes)
        if inventory:
            logger.info("using %d statically configured overcloud node(s)", len(inventory))
            self._inventory = inventory
            return inventory

        version = await self.detect_version()
        commands = self.INVENTORY_COMMANDS.get(version, self.INVENTORY_FALLBACK)

        errors: list[str] = []
        for command in commands:
            try:
                result = await self.run_director(command)
            except CommandNotAllowed as exc:
                logger.info("inventory source not allow-listed (%s): %s", command, exc)
                errors.append(f"{command}: not allow-listed")
                continue
            if not result.ok:
                errors.append(f"{command}: {result.stderr[:160]}")
                logger.info("inventory source unavailable (%s)", errors[-1])
                continue
            parsed = _parse_inventory(result.stdout)
            if parsed:
                logger.info("resolved %d overcloud node(s) via `%s`", len(parsed), command)
                self._inventory = parsed
                return parsed
            errors.append(f"{command}: returned no usable host/address pairs")

        logger.warning("overcloud inventory lookup failed: %s", "; ".join(errors))
        self._inventory = {}
        return self._inventory

    def _normalize(self, host: str) -> str:
        return host.strip().lower().rstrip(".")

    def _without_domain(self, host: str) -> str:
        """Drop the configured domain suffix, if this host carries it."""
        host = self._normalize(host)
        domain = self.director.domain.strip().lower().lstrip(".")
        if domain and host.endswith(f".{domain}"):
            return host[: -len(domain) - 1]
        return host

    async def resolve_node(self, node: str) -> str | None:
        """Map an alarm's host label to a ctlplane IP.

        Alarm host labels are FQDNs (`...ctrl03.rhosp2.jiocaas.com`) while metalsmith and Nova
        may report either form. Matching is tried most-specific first and every comparison is an
        equality: substring matching would happily resolve `ctrl0` to `ctrl01` and send commands
        to the wrong node. The first-label comparison is only trusted when exactly one inventory
        host has that label, so `node.rack1` and `node.rack2` can never be confused.
        """
        inventory = await self.list_nodes()
        wanted = self._normalize(node)

        for name, ip in inventory.items():
            if self._normalize(name) == wanted:
                return ip

        stripped = self._without_domain(node)
        for name, ip in inventory.items():
            if self._without_domain(name) == stripped:
                return ip

        label = stripped.split(".")[0]
        matches = [ip for name, ip in inventory.items()
                   if self._without_domain(name).split(".")[0] == label]
        if len(matches) == 1:
            return matches[0]
        if matches:
            logger.warning(
                "host label %r matches %d inventory entries on its first label; refusing to guess",
                node, len(matches),
            )
        # Fallback: try director DNS resolution when inventory is empty.
        resolved = await self._resolve_via_director(node)
        if resolved:
            logger.info(
                "resolved node %r via director DNS -> %s (added to ephemeral inventory)",
                node, resolved,
            )
            self._inventory[node] = resolved
            return resolved
        return None

    async def _resolve_via_director(self, node: str) -> str | None:
        """Resolve a node hostname via the director's DNS (undercloud dnsmasq).

        Tries ``getent hosts <fqdn>`` first, then ``host <fqdn>``.
        """
        candidates = []
        raw = node.strip()
        if raw:
            candidates.append(raw)
        if "." not in raw and self.director.domain:
            domain = self.director.domain.strip().lstrip(".")
            candidates.append(f"{raw}.{domain}")

        for hostname in candidates:
            cmd = f"getent hosts {hostname}"
            try:
                self.allowlist.validate(cmd, scope="director")
            except CommandNotAllowed:
                continue
            result = await self.run_director(cmd)
            if not result.ok:
                continue
            logger.info(
                "DNS lookup for %r: getent exit=0 stdout (first 300 chars): %r",
                hostname, result.stdout[:300],
            )
            ip = _parse_getent_result(result.stdout)
            if ip:
                return ip

        for hostname in candidates:
            cmd = f"host {hostname}"
            try:
                self.allowlist.validate(cmd, scope="director")
            except CommandNotAllowed:
                continue
            result = await self.run_director(cmd)
            if not result.ok:
                continue
            ip = _parse_host_result(result.stdout)
            if ip:
                return ip

        return None

    async def run_node(self, node: str, command: str) -> CommandResult:
        """Run an allow-listed read-only command on an overcloud node via the director hop."""
        validated = self.allowlist.validate(command, scope="overcloud")
        ip = await self.resolve_node(node)
        user = await self.overcloud_user()

        if ip:
            # IPv6 addresses need brackets for SSH: [2405:200::1]
            ssh_ip = f"{ip}" if ":" in ip else ip
            hop = (
                f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
                f"{user}@{ssh_ip} {shlex.quote(validated)}"
            )
            logger.info("node command on %s (%s): %s", node, ip, validated)
        else:
            # No IP from inventory or DNS — try SSH by hostname.
            # The director's dnsmasq resolves overcloud FQDNs to ctlplane IPs.
            logger.info(
                "node command via hostname SSH (no IP): %s / %s",
                node, validated,
            )
            hop = (
                f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
                f"{user}@{node} {shlex.quote(validated)}"
            )

        wrapped = f"sudo -u {shlex.quote(self.director.stack_user)} -i bash -c {shlex.quote(hop)}"
        return await self._run_raw(wrapped, target=node)
