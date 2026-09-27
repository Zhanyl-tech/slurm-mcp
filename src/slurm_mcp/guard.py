"""The allowlist, enforced in code rather than in the prompt.

A system prompt that says "only use read-only commands" is a request, not a
control. It fails open: a jailbreak, a confused tool call, or an ordinary
hallucination is enough to reach ``scontrol update`` on a production
controller. Anything that could drain a node must be impossible to *express*,
not merely discouraged.

v0.1.0 did this with a denylist of known-bad subcommands, and a list of bad
things is exactly what Slurm defeats: ``scontrol`` accepts any unique
abbreviation (``scontrol upd`` is ``update``), options with values can sit
before the subcommand (``scontrol -u 0 shutdown``), and the list missed real
verbs and options (``uhold``, ``token``, ``sdiag --reset``). An audit found 22
such argv that passed, plus a 23rd: v0.1.0 let any caller value starting with
``--format=`` skip the metacharacter check. ``tests/test_guard.py`` keeps all 23.

So the check is now an allowlist of *shapes*, in two layers that must both
pass, and both refuse by default:

1. **Binary shape** (:data:`ALLOWED`). Only these binaries may run, and each may
   carry only the exact option spellings listed for it — no abbreviations, no
   bundled short options, no alternative spelling Slurm would also accept.
   ``scontrol`` may only be ``scontrol show <config|job|node|partition> [name]``;
   ``sdiag`` takes no options at all. This layer is written by hand and does not
   read the topic table, so it also checks the topic table: a topic edited to
   run ``scontrol update`` is refused here.
2. **Topic shape**. The argv must be exactly one topic's command
   (:data:`slurm_mcp.topics.TOPICS`) followed by ``flag value`` pairs using that
   topic's filter flags, where every value is a plain name, list, state or time.
   Caller-supplied values never land in a format-string position, so the only
   argument allowed to contain ``|`` is one this package wrote.

Commands are also executed with ``shell=False``, which makes the character
rules defence in depth rather than the only barrier. And none of this replaces
Slurm's own authorization: run the server as an unprivileged Slurm account (see
the README's Deployment section) so that Slurm refuses what a guard bug lets by.

The threat model is deliberately not "malicious user". It is an agent that has
read a confusing log line at 3am and is about to do something decisive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .topics import FILTER_VALUE, TOPICS


class Denied(Exception):
    """Raised when a command is not expressible through this server."""


class Arg(Enum):
    """What an allowlisted option is followed by."""

    #: Takes no value.
    SWITCH = "switch"
    #: Takes one value that must match :data:`slurm_mcp.topics.FILTER_VALUE`.
    VALUE = "value"
    #: Takes a Slurm output-format string (:data:`FORMAT_VALUE`).
    FORMAT = "format"


#: A Slurm format string as this package writes them: ``%i|%P|%u`` for the
#: printf-style tools, ``JobID,Account,User`` for sacct and sshare. Pipes are the
#: column separator and are harmless under ``shell=False``; ``$``, backticks,
#: quotes, spaces and parentheses are not needed and are not allowed.
FORMAT_VALUE = re.compile(r"[A-Za-z0-9%|,.:_]{1,256}")


@dataclass(frozen=True)
class Binary:
    binary: str
    description: str
    #: Exact option spellings permitted, and what follows each. Anything not
    #: listed is refused, including abbreviations Slurm itself would accept.
    options: dict[str, Arg] = field(default_factory=dict)
    #: ``scontrol`` only: the entities ``scontrol show`` may read.
    show_entities: frozenset[str] = frozenset()


#: Adding to this table is a deliberate act. The default answer to "should an
#: agent be able to run this?" is no. Each option below is used by a topic; the
#: short value options are all declared ``required_argument`` in the binary's
#: getopt table upstream, so a value can never be read as another option.
ALLOWED: dict[str, Binary] = {
    "sinfo": Binary(
        "sinfo",
        "Node and partition state",
        options={
            "--noheader": Arg.SWITCH,
            "-N": Arg.SWITCH,
            "-o": Arg.FORMAT,
            "-p": Arg.VALUE,
            "-t": Arg.VALUE,
        },
    ),
    "squeue": Binary(
        "squeue",
        "Job queue",
        options={
            "--noheader": Arg.SWITCH,
            "-o": Arg.FORMAT,
            "-u": Arg.VALUE,
            "-p": Arg.VALUE,
            "-t": Arg.VALUE,
        },
    ),
    "sacct": Binary(
        "sacct",
        "Accounting history",
        options={
            "-a": Arg.SWITCH,
            "-X": Arg.SWITCH,
            "--parsable2": Arg.SWITCH,
            "--noheader": Arg.SWITCH,
            "--format": Arg.FORMAT,
            "-u": Arg.VALUE,
            "-S": Arg.VALUE,
            "-E": Arg.VALUE,
            "-s": Arg.VALUE,
        },
    ),
    # No options at all: `sdiag -r` / `--reset` zeroes the scheduler and RPC
    # counters, and getopt_long would also accept any unique prefix of --reset.
    "sdiag": Binary("sdiag", "Scheduler diagnostics — cycle counts, RPC and agent queues"),
    "sprio": Binary(
        "sprio",
        "Priority breakdown per pending job",
        options={
            "--noheader": Arg.SWITCH,
            "-o": Arg.FORMAT,
            "-u": Arg.VALUE,
            "-p": Arg.VALUE,
        },
    ),
    "sshare": Binary(
        "sshare",
        "Fairshare state",
        options={
            "--noheader": Arg.SWITCH,
            "--parsable2": Arg.SWITCH,
            "-a": Arg.SWITCH,
            "--format": Arg.FORMAT,
            "-u": Arg.VALUE,
            "-A": Arg.VALUE,
        },
    ),
    # scontrol is the sharpest object in the drawer: `show` is harmless and
    # `update` drains nodes. It is permitted in exactly one shape rather than
    # excluded, since `scontrol show config` is often the fastest route to a
    # diagnosis. No options: `-M` and `-u` take a value and would let a verb
    # hide behind it, and a bare `scontrol` reads commands from stdin.
    "scontrol": Binary(
        "scontrol",
        "Cluster state: `scontrol show` only",
        show_entities=frozenset({"config", "job", "node", "partition"}),
    ),
}


def _check_value(binary: str, flag: str, kind: Arg, value: str) -> None:
    if kind is Arg.FORMAT:
        if not FORMAT_VALUE.fullmatch(value):
            raise Denied(f"{binary} {flag}: {value!r} is not a plain Slurm format string")
    elif not FILTER_VALUE.fullmatch(value):
        raise Denied(
            f"{binary} {flag}: {value!r} is not a plain name, list, state or time "
            f"(it starts with '-', or contains a shell metacharacter, whitespace or a "
            f"control character)"
        )


def _check_scontrol(args: list[str], spec: Binary) -> None:
    shape = f"scontrol show <{'|'.join(sorted(spec.show_entities))}> [name]"
    if not args:
        raise Denied(f"bare scontrol reads commands from stdin; only {shape} is permitted")
    if args[0] != "show" or len(args) < 2 or args[1] not in spec.show_entities:
        raise Denied(
            f"scontrol {' '.join(args)!r} is not permitted; only {shape} is, spelled in "
            f"full with no options. This server reads, it does not act."
        )
    if len(args) > 3 or (len(args) == 3 and args[1] == "config"):
        raise Denied(f"scontrol show takes one entity and at most one name; got {args!r}")
    if len(args) == 3:
        _check_value("scontrol", "show", Arg.VALUE, args[2])


def check_binary_shape(argv: list[str]) -> Binary:
    """Layer 1: the binary is allowlisted and every argument is an exact option."""
    binary = argv[0]
    spec = ALLOWED.get(binary)
    if spec is None:
        raise Denied(
            f"{binary!r} is not on the read-only allowlist; permitted: {', '.join(sorted(ALLOWED))}"
        )
    args = argv[1:]
    if binary == "scontrol":
        _check_scontrol(args, spec)
        return spec

    i = 0
    while i < len(args):
        token = args[i]
        kind = spec.options.get(token)
        if kind is Arg.SWITCH:
            i += 1
            continue
        if kind is not None:
            if i + 1 >= len(args):
                raise Denied(f"{binary} {token} needs a value")
            _check_value(binary, token, kind, args[i + 1])
            i += 2
            continue
        # `--format=VALUE`: the attached form of a long option that takes a value.
        name, sep, attached = token.partition("=")
        attached_kind = spec.options.get(name) if token.startswith("--") and sep else None
        if attached_kind is Arg.VALUE or attached_kind is Arg.FORMAT:
            _check_value(binary, name, attached_kind, attached)
            i += 1
            continue
        raise Denied(
            f"{binary}: {token!r} is not on this binary's option allowlist "
            f"({', '.join(sorted(spec.options)) or 'no options'}); abbreviations and "
            f"positional arguments are refused"
        )
    return spec


def check_topic_shape(argv: list[str]) -> None:
    """Layer 2: argv is a topic's command followed by that topic's filter pairs."""
    closest: str | None = None
    for topic in TOPICS.values():
        base = list(topic.argv)
        if argv[: len(base)] != base:
            continue
        rest = argv[len(base) :]
        flags = set(topic.filters.values())
        if len(rest) % 2:
            closest = f"topic {topic.name!r}: filters must be flag/value pairs, got {rest!r}"
            continue
        for flag, value in zip(rest[::2], rest[1::2], strict=True):
            if flag not in flags:
                closest = (
                    f"topic {topic.name!r} has no filter flag {flag!r}; "
                    f"accepts {', '.join(sorted(flags)) or 'none'}"
                )
                break
            if not FILTER_VALUE.fullmatch(value):
                closest = f"topic {topic.name!r}: filter value {value!r} is not a plain value"
                break
        else:
            return
    raise Denied(closest or f"{' '.join(argv)!r} is not a command any topic builds")


def guard(argv: list[str]) -> None:
    """Raise :class:`Denied` unless ``argv`` is a permitted read-only command."""
    if not argv:
        raise Denied("empty command")
    check_binary_shape(argv)
    check_topic_shape(argv)


def is_allowed(argv: list[str]) -> bool:
    try:
        guard(argv)
    except Denied:
        return False
    return True
