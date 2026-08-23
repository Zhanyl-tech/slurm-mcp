"""The allowlist, enforced in code rather than in the prompt.

A system prompt that says "only use read-only commands" is a request, not a
control. It fails open: a jailbreak, a confused tool call, or an ordinary
hallucination is enough to reach ``scontrol update`` on a production
controller. Anything that could drain a node must be impossible to *express*,
not merely discouraged.

So the check lives here and runs on every invocation:

* only binaries in :data:`ALLOWED` may run at all;
* each carries forbidden subcommands and flags — ``scontrol`` is permitted for
  ``show`` and refused for ``update``, ``reconfigure``, ``shutdown``;
* shell metacharacters are refused outright, so ``sinfo; rm -rf /`` cannot be
  smuggled through an argument. Commands are executed without a shell anyway,
  which makes this defence in depth rather than the only barrier.

The threat model is deliberately not "malicious user". It is an agent that has
read a confusing log line at 3am and is about to do something decisive.

This mirrors the surface inside `cluster-sre-agent`; this repo is the
standalone server, so any MCP client can be given the same guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Characters that could chain, redirect, or substitute another command.
SHELL_METACHARACTERS = re.compile(r"[;&|<>`$(){}\[\]\\\n\r]")


class Denied(Exception):
    """Raised when a command is not expressible through this server."""


@dataclass(frozen=True)
class Tool:
    binary: str
    description: str
    forbidden_subcommands: frozenset[str] = field(default_factory=frozenset)
    forbidden_flags: frozenset[str] = field(default_factory=frozenset)


#: Adding to this list is a deliberate act. The default answer to "should an
#: agent be able to run this?" is no.
ALLOWED: dict[str, Tool] = {
    "sinfo": Tool("sinfo", "Node and partition state"),
    "squeue": Tool("squeue", "Job queue"),
    "sacct": Tool("sacct", "Accounting history"),
    "sdiag": Tool("sdiag", "Scheduler diagnostics — cycle counts, RPC and agent queues"),
    "sprio": Tool("sprio", "Priority breakdown per pending job"),
    "sshare": Tool("sshare", "Fairshare state"),
    "sacctmgr": Tool(
        "sacctmgr",
        "Accounting associations and limits, read paths only",
        forbidden_subcommands=frozenset(
            {"add", "create", "delete", "remove", "modify", "update", "archive", "dump", "load"}
        ),
    ),
    "scontrol": Tool(
        "scontrol",
        "Cluster state, read paths only",
        # scontrol is the sharpest object in the drawer: `show` is harmless and
        # `update` drains nodes. Permitted narrowly rather than excluded, since
        # `scontrol show config` is often the fastest route to a diagnosis.
        forbidden_subcommands=frozenset(
            {
                "update",
                "delete",
                "create",
                "reconfigure",
                "shutdown",
                "takeover",
                "requeue",
                "requeuehold",
                "hold",
                "release",
                "suspend",
                "resume",
                "cancel",
                "setdebug",
                "setdebugflags",
                "write",
                "notify",
                "reboot",
                "wait_job",
                "top",
                "power",
            }
        ),
    ),
}

#: Flags refused on every binary. `--help` is fine; running a program is not.
GLOBAL_FORBIDDEN_FLAGS = frozenset({"-e", "--exec", "--wrap"})

#: Flags whose *value* is a Slurm output-format string. Those legitimately
#: contain `|` as a column separator, which the metacharacter rule would
#: otherwise refuse. Exempting the value is safe and narrow: commands run with
#: shell=False, so a pipe in an argument is an ordinary character, and the
#: format strings are built from this package's closed topic vocabulary rather
#: than from anything a caller supplies. The strict check still applies to
#: every other argument, including all user-supplied filter values.
FORMAT_FLAGS = frozenset({"-o", "--format", "-O", "--Format"})


def guard(argv: list[str]) -> None:
    """Raise :class:`Denied` unless ``argv`` is a permitted read-only command."""
    if not argv:
        raise Denied("empty command")

    binary = argv[0]
    tool = ALLOWED.get(binary)
    if tool is None:
        raise Denied(
            f"{binary!r} is not on the read-only allowlist; permitted: {', '.join(sorted(ALLOWED))}"
        )

    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in FORMAT_FLAGS:
            skip_next = True
            continue
        if arg.split("=", 1)[0] in FORMAT_FLAGS and "=" in arg:
            continue
        if SHELL_METACHARACTERS.search(arg):
            raise Denied(f"argument {arg!r} contains a shell metacharacter")

    positional = [a for a in argv[1:] if not a.startswith("-")]
    if positional:
        sub = positional[0].lower()
        if sub in tool.forbidden_subcommands:
            raise Denied(
                f"{binary} {sub!r} mutates cluster state and is not permitted; "
                f"this server reads, it does not act"
            )

    for arg in argv[1:]:
        # Case-sensitive on purpose. Unix flags are: lowercasing here made
        # `sacct -E <endtime>` collide with the forbidden `-e`, refusing a
        # perfectly ordinary accounting read.
        flag = arg.split("=", 1)[0]
        if flag in GLOBAL_FORBIDDEN_FLAGS or flag in tool.forbidden_flags:
            raise Denied(f"{binary} flag {flag!r} is not permitted")


def is_allowed(argv: list[str]) -> bool:
    try:
        guard(argv)
    except Denied:
        return False
    return True
