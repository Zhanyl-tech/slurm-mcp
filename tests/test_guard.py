"""Adversarial tests for the read-only guarantee.

These drive the guard with real mutating commands rather than asserting on
prompt text. A prompt-level promise cannot be tested; this can.
"""

from __future__ import annotations

import pytest

from slurm_mcp.guard import ALLOWED, Denied, guard, is_allowed

# Every mutating invocation that has a plausible path through an agent.
MUTATIONS = [
    ["scontrol", "update", "NodeName=ALL", "State=DRAIN"],
    ["scontrol", "update", "JobId=123", "TimeLimit=0"],
    ["scontrol", "reconfigure"],
    ["scontrol", "shutdown"],
    ["scontrol", "takeover"],
    ["scontrol", "requeue", "123"],
    ["scontrol", "hold", "123"],
    ["scontrol", "release", "123"],
    ["scontrol", "suspend", "123"],
    ["scontrol", "reboot", "dgx8-01"],
    ["scontrol", "delete", "PartitionName=gpu"],
    ["scontrol", "power", "down", "dgx8-01"],
    ["sacctmgr", "modify", "user", "alice", "set", "maxjobs=0"],
    ["sacctmgr", "delete", "account", "research"],
    ["sacctmgr", "add", "user", "mallory"],
    ["sacctmgr", "load", "dump.cfg"],
]

NOT_ON_ALLOWLIST = [
    ["scancel", "123"],
    ["srun", "hostname"],
    ["sbatch", "job.sh"],
    ["salloc"],
    ["rm", "-rf", "/"],
    ["bash", "-c", "echo hi"],
    ["sh"],
    ["systemctl", "restart", "slurmctld"],
    ["ssh", "dgx8-01"],
]

INJECTIONS = [
    ["sinfo", "; rm -rf /"],
    ["squeue", "-u", "alice; scancel -u alice"],
    ["sinfo", "$(scancel -u alice)"],
    ["sinfo", "`scancel -u alice`"],
    ["squeue", "-p", "gpu && scontrol update NodeName=ALL State=DRAIN"],
    ["sinfo", "-p", "gpu | tee /etc/passwd"],
    ["sacct", "-u", "alice\nscancel -u alice"],
    ["sinfo", "-p", "gpu > /etc/slurm/slurm.conf"],
]


@pytest.mark.parametrize("argv", MUTATIONS, ids=lambda a: " ".join(a)[:40])
def test_mutating_commands_are_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


@pytest.mark.parametrize("argv", NOT_ON_ALLOWLIST, ids=lambda a: " ".join(a)[:40])
def test_binaries_off_the_allowlist_are_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


@pytest.mark.parametrize("argv", INJECTIONS, ids=lambda a: " ".join(a)[:40])
def test_shell_injection_through_arguments_is_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


READS = [
    ["sinfo"],
    ["sinfo", "-p", "gpu"],
    ["squeue", "-u", "alice"],
    ["squeue", "--noheader", "-o", "%i|%P|%u|%T"],
    ["scontrol", "show", "config"],
    ["scontrol", "show", "node", "dgx8-01"],
    ["scontrol", "show", "job", "123"],
    ["sacct", "-a", "-X", "--parsable2"],
    ["sacctmgr", "show", "assoc"],
    ["sacctmgr", "list", "user"],
    ["sdiag"],
    ["sprio", "-u", "alice"],
    ["sshare", "-a"],
]


@pytest.mark.parametrize("argv", READS, ids=lambda a: " ".join(a)[:40])
def test_legitimate_reads_are_allowed(argv: list[str]) -> None:
    guard(argv)


def test_format_strings_may_contain_pipes() -> None:
    """Slurm format specifiers use `|`; commands never touch a shell.

    Narrow exemption: only the value of a format flag. The strict rule still
    covers every other argument, which is what the injection tests prove.
    """
    guard(["squeue", "-o", "%i|%P|%u|%T|%M|%D|%R", "-u", "alice"])
    assert not is_allowed(["squeue", "-u", "alice|scancel"])


def test_exemption_does_not_leak_to_the_next_flag() -> None:
    """`-o` exempts exactly one value, not the remainder of the command."""
    assert not is_allowed(["squeue", "-o", "%i", "-u", "; rm -rf /"])


def test_empty_command_is_refused() -> None:
    with pytest.raises(Denied):
        guard([])


def test_scontrol_show_survives_because_it_is_the_useful_one() -> None:
    """scontrol is permitted narrowly rather than excluded outright."""
    assert is_allowed(["scontrol", "show", "config"])
    assert not is_allowed(["scontrol", "update", "NodeName=x", "State=DRAIN"])


def test_every_allowlisted_binary_is_a_read_tool() -> None:
    assert set(ALLOWED) == {
        "sinfo",
        "squeue",
        "sacct",
        "sdiag",
        "sprio",
        "sshare",
        "sacctmgr",
        "scontrol",
    }


def test_flag_matching_is_case_sensitive() -> None:
    """`sacct -E` is endtime; `-e` is the forbidden exec-ish flag.

    Lowercasing the flag before comparison conflated them and refused a normal
    accounting read.
    """
    assert is_allowed(["sacct", "-E", "2026-08-23T00:00:00"])
    assert is_allowed(["sacct", "-S", "2026-08-01", "-E", "2026-08-23"])
    assert not is_allowed(["sacct", "-e"])
