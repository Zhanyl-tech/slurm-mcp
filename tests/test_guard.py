"""Adversarial tests for the read-only guarantee.

These drive the guard with real mutating commands rather than asserting on
prompt text. A prompt-level promise cannot be tested; this can.

The corpus has five parts. BYPASSES is the one that matters most: 23 argv that
passed v0.1.0's denylist and should not have, found by an audit that read the
Slurm man pages and the SchedMD/slurm master source. 22 of them come from
Slurm's own behaviour (a write, a minted credential, a counter reset, an
interactive prompt), and each of those cites the man page or source line it
relies on, so a future change that re-opens one fails here with the reason
attached. The 23rd is v0.1.0's own `--format=` exemption; its comment says why
it is different.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from slurm_mcp import guard as guard_module
from slurm_mcp.guard import (
    ALLOWED,
    Denied,
    check_binary_shape,
    check_topic_shape,
    guard,
    is_allowed,
)
from slurm_mcp.topics import TOPICS, Topic, build_argv

README = Path(__file__).resolve().parent.parent / "README.md"

# Man pages: Slurm 26.05, https://slurm.schedmd.com/<binary>.html, read 2026-09-26.
# Source lines: SchedMD/slurm master as fetched by the audit on 2026-09-26.
BYPASSES = [
    # --- (a) abbreviation. scontrol(1): "All commands and options can be
    # abbreviated to the extent that the specification is unique."
    # scontrol.c:1607 matches update on MAX(tag_len, 1), so any prefix is update.
    ["scontrol", "upd", "NodeName=gpu-01", "State=DRAIN", "Reason=x"],
    ["scontrol", "u", "JobId=123", "TimeLimit=0"],
    # scontrol.c:1369 matches reconfigure on MAX(tag_len, 3).
    ["scontrol", "reconf"],
    # --- (b) an option with a value before the subcommand. scontrol.c:194
    # getopt "adhM:FoQu:vV": -M and -u consume the next word, so the verb is not
    # the first non-dash word any more.
    ["scontrol", "-M", "mycluster", "update", "NodeName=ALL", "State=DRAIN"],
    ["scontrol", "-u", "0", "shutdown"],
    # --- (c) documented state-changing verbs v0.1.0's list did not name.
    # scontrol(1) uhold/holdu: user-level hold (scontrol.c:1408-1409).
    ["scontrol", "uhold", "123"],
    ["scontrol", "holdu", "123"],
    # scontrol(1) reboot_nodes / cancel_reboot (scontrol.c:1367, :1134).
    ["scontrol", "reboot_nodes", "gpu-01"],
    ["scontrol", "cancel_reboot", "gpu-01"],
    # scontrol(1) schedloglevel: changes scheduler logging (scontrol.c:1476).
    ["scontrol", "schedloglevel", "0"],
    # scontrol(1) fsdampeningfactor: sets FairShareDampeningFactor (scontrol.c:1452).
    ["scontrol", "fsdampeningfactor", "5"],
    # scontrol(1) token: "Returns an authentication token for JWT support"
    # (scontrol.c:1438). Minting credentials is not a read.
    ["scontrol", "token", "username=root", "lifespan=infinite"],
    # sacctmgr(1) shutdown: "Shutdown the server." (sacctmgr.c:668)
    ["sacctmgr", "shutdown"],
    # sacctmgr(1) reconfigure: "Reconfigures the SlurmDBD" (sacctmgr.c:619)
    ["sacctmgr", "-i", "reconfigure"],
    # sacctmgr(1) clear stats: "Clear the server statistics." (sacctmgr.c:577)
    ["sacctmgr", "clear", "stats"],
    # sacctmgr.c:582 matches modify/update on MAX(command_len, 1); -i commits
    # "immediately without asking for confirmation" (sacctmgr(1)).
    ["sacctmgr", "-i", "mod", "user", "alice", "set", "maxjobs=0"],
    # sacctmgr.c:585-588 matches remove/delete on MAX(command_len, 3).
    ["sacctmgr", "-i", "rem", "account", "research"],
    # sdiag(1) -r, --reset: "Reset scheduler and RPC counters to 0." Resetting
    # counters destroys the evidence a diagnosis would read.
    # sdiag opts.c:81 declares {"reset", no_argument, 0, 'r'}.
    ["sdiag", "-r"],
    ["sdiag", "--reset"],
    # sdiag opts.c:103 parses with getopt_long, and getopt_long(3): "Long option
    # names may be abbreviated if the abbreviation is unique or is an exact
    # match for some defined option." (man7.org, man-pages 6.19, read 2026-09-26)
    ["sdiag", "--res"],
    # --- interactive mode. scontrol(1): with no command "scontrol will operate
    # in an interactive mode and prompt for input" (scontrol.c:378, fgets from
    # stdin); sacctmgr(1): "enters interactive mode" likewise.
    ["scontrol"],
    ["sacctmgr"],
    # --- not upstream behaviour, and not state-changing: v0.1.0 exempted any
    # argument *starting with* --format= from the metacharacter check,
    # including caller-supplied filter values. Unlike the 22 above, this one was
    # reachable through slurm_query (queue, priority and accounting all built
    # it). It was harmless: -u takes a required argument (squeue opts.c:163,
    # sprio opts.c:121, sacct options.c:507), so the value stayed a user list,
    # and there was no shell (shell=False).
    ["squeue", "-u", "--format=$(scancel -u alice);`id`"],
]

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
    ["scontrol", "reboot", "gpu-01"],
    ["scontrol", "delete", "PartitionName=gpu"],
    ["scontrol", "power", "down", "gpu-01"],
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
    ["ssh", "gpu-01"],
    ["/usr/bin/scontrol", "show", "config"],
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
    ["squeue", "-u", "a\x00b"],
    ["squeue", "-u", "alice\rscancel"],
    ["squeue", "-u", "alice bob"],
]

# Shapes a denylist would miss and an allowlist refuses by construction:
# spellings Slurm would accept, and reads outside the vocabulary.
OFF_SHAPE = [
    ["scontrol", "sh", "config"],
    ["scontrol", "SHOW", "config"],
    ["scontrol", "show", "config", "extra"],
    ["scontrol", "show", "reservation"],
    ["scontrol", "-o", "show", "config"],
    ["squeue", "--noheader", "-o", "%i", "--form=%i"],
    ["sacct", "--parsable"],
    ["sinfo", "-Np", "gpu"],
    ["squeue", "-u", "-t"],
    ["squeue", "-u"],
]

ADVERSARIAL = BYPASSES + MUTATIONS + NOT_ON_ALLOWLIST + INJECTIONS + OFF_SHAPE


def _id(argv: list[str]) -> str:
    return " ".join(argv)[:48] or "(empty)"


def test_the_audit_found_twenty_three_bypasses() -> None:
    assert len(BYPASSES) == 23


@pytest.mark.parametrize("argv", BYPASSES, ids=_id)
def test_audit_bypasses_are_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


@pytest.mark.parametrize("argv", BYPASSES, ids=_id)
def test_each_layer_alone_refuses_every_bypass(argv: list[str]) -> None:
    """Defence in depth: either layer on its own would have stopped all 23."""
    with pytest.raises(Denied):
        check_binary_shape(argv)
    with pytest.raises(Denied):
        check_topic_shape(argv)


@pytest.mark.parametrize("argv", MUTATIONS, ids=_id)
def test_mutating_commands_are_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


@pytest.mark.parametrize("argv", NOT_ON_ALLOWLIST, ids=_id)
def test_binaries_off_the_allowlist_are_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


@pytest.mark.parametrize("argv", INJECTIONS, ids=_id)
def test_shell_injection_through_arguments_is_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


@pytest.mark.parametrize("argv", OFF_SHAPE, ids=_id)
def test_shapes_outside_the_allowlist_are_refused(argv: list[str]) -> None:
    with pytest.raises(Denied):
        guard(argv)


def test_readme_states_the_size_of_the_adversarial_corpus() -> None:
    """The README number is produced by this list, not typed from memory."""
    match = re.search(r"(\d+) adversarial argv", README.read_text(encoding="utf-8"))
    assert match, "README should state how many adversarial argv the tests drive"
    assert int(match.group(1)) == len(ADVERSARIAL)


def test_every_topic_command_is_allowed_with_and_without_filters() -> None:
    for name, topic in TOPICS.items():
        guard(build_argv(name))
        for key in topic.filters:
            guard(build_argv(name, {key: "alice"}))
        guard(build_argv(name, dict.fromkeys(topic.filters, "gpu")))


def test_reads_outside_the_vocabulary_are_refused() -> None:
    """v0.1.0 allowed any read a binary offered; now only the topics' shapes run."""
    for argv in (
        ["sinfo"],
        ["sinfo", "-p", "gpu"],
        ["squeue", "-u", "alice"],
        ["scontrol", "show", "node", "gpu-01"],
        ["sacctmgr", "show", "assoc"],
    ):
        assert not is_allowed(argv), argv


def test_format_strings_may_contain_pipes_only_where_the_package_wrote_them() -> None:
    """Slurm format specifiers use `|`; a caller value may not."""
    guard(build_argv("queue", {"user": "alice"}))
    assert not is_allowed(build_argv("queue") + ["-u", "alice|scancel"])
    assert not is_allowed(build_argv("queue") + ["-o", "%i|%u"])


def test_empty_command_is_refused() -> None:
    with pytest.raises(Denied):
        guard([])


def test_scontrol_is_permitted_in_exactly_one_spelling() -> None:
    assert is_allowed(["scontrol", "show", "config"])
    assert not is_allowed(["scontrol", "update", "NodeName=x", "State=DRAIN"])
    check_binary_shape(["scontrol", "show", "node", "gpu-01"])  # layer 1 knows the shape
    with pytest.raises(Denied):
        check_binary_shape(["scontrol", "show", "node", "gpu-01; scancel 1"])


def test_every_allowlisted_binary_is_a_read_tool() -> None:
    """sacctmgr was removed: no topic uses it, and its verbs abbreviate to writes."""
    assert set(ALLOWED) == {"sinfo", "squeue", "sacct", "sdiag", "sprio", "sshare", "scontrol"}


def test_flag_matching_is_case_sensitive() -> None:
    """`sacct -E` is --endtime and `-e` is --helpformat (sacct options.c).

    v0.1.0 refused `-e` as an "exec-ish" flag; none of these binaries has an
    exec flag, so that rule guarded nothing. Case still matters: lowercasing once
    made `-E <endtime>` collide with it.
    """
    assert is_allowed(build_argv("accounting", {"endtime": "2026-08-23T00:00:00"}))
    assert is_allowed(build_argv("accounting", {"starttime": "2026-08-01", "endtime": "now"}))
    assert not is_allowed(build_argv("accounting") + ["-e"])


def test_a_topic_edited_to_mutate_is_refused_by_the_binary_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail safe: widening the vocabulary does not widen what may run.

    The topic layer trusts TOPICS by design, so a bad edit there would pass it.
    The binary layer is written by hand and does not read TOPICS, which is what
    catches this.
    """
    bad = {
        "drain": Topic("drain", "x", ("scontrol", "update", "NodeName=ALL", "State=DRAIN")),
        "reset": Topic("reset", "x", ("sdiag", "--reset")),
        "cancel": Topic("cancel", "x", ("scancel", "-u", "alice")),
    }
    monkeypatch.setattr(guard_module, "TOPICS", {**TOPICS, **bad})
    for topic in bad.values():
        argv = list(topic.argv)
        check_topic_shape(argv)  # the vocabulary now "allows" it...
        with pytest.raises(Denied):
            guard(argv)  # ...and the guard still refuses it.


def test_the_topic_layer_checks_values_on_its_own() -> None:
    """Even an argv that reaches the topic layer directly gets its values checked."""
    base = build_argv("queue")
    with pytest.raises(Denied, match="not a plain value"):
        check_topic_shape([*base, "-u", "alice bob"])
    with pytest.raises(Denied, match="has no filter flag"):
        check_topic_shape([*base, "-A", "research"])
