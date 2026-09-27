"""Progressive disclosure: three tools instead of seven, detail on request.

The obvious MCP server for Slurm exposes one tool per binary — `sinfo`,
`squeue`, `sacct`, `sdiag`, `sprio`, `sshare`, `scontrol` — each with a schema
describing its flags. That is the design this repo argues against. The
argument is about where per-flag guidance lives: in a flat server it sits in
the tool descriptions, in the model's context on every turn, whether the
question is about the queue or not. Here it is fetched on request.

``slurm-mcp footprint`` measures this, and the result is modest. A flat server
with generic schemas and no guidance is about the same resident size as this
one. The larger ratio holds only if a flat server carried guidance comparable
to ``slurm_describe``, an assumption not measured against any real server. The
README gives the numbers, and a test keeps them in step with the code.

So the server exposes three tools:

* ``slurm_overview`` — one cheap snapshot answering "what is the cluster doing",
  the question most sessions open with.
* ``slurm_query`` — a topic plus optional filters. Topics are a short closed
  vocabulary, not a command line.
* ``slurm_describe`` — the flags and output shape for *one* topic, fetched only
  when the agent has decided it needs them.

The agent pays for detail when it asks for it. Nothing here prevents a client
from being wasteful, but the default path is cheap.

The topic table is also the allowlist: :mod:`slurm_mcp.guard` refuses any argv
that is not exactly a topic's command followed by that topic's filter flags.

Column layouts below follow the Slurm 26.05 man pages (checked 2026-09-26). They
have not been verified against a live cluster's output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: What a caller-supplied filter value may look like: user, account and
#: partition names, comma lists, job states, and Slurm time expressions such as
#: ``2026-08-01T00:00:00`` or ``now-1days``. It is an allowlist of characters,
#: not a list of dangerous ones, so a value cannot start with ``-`` (and so
#: cannot become an option even if a flag were ever declared optional-argument
#: upstream), and cannot carry a shell metacharacter, whitespace, NUL or any
#: other control character. Commands run with ``shell=False`` regardless; this
#: is what keeps the argv an agent sees in a response equal to the argv a human
#: would have to type.
FILTER_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.,:@+-]{0,127}")


class InvalidFilter(ValueError):
    """A filter value that is not a plain name, list, state or time."""


@dataclass(frozen=True)
class Topic:
    """One question an operator asks, and the read that answers it."""

    name: str
    summary: str
    #: Base command. Filters are appended by the caller, never interpolated.
    argv: tuple[str, ...]
    #: Filters this topic understands, mapped to the flag they become.
    filters: dict[str, str] = field(default_factory=dict)
    #: Column names for pipe-delimited output, in order. Empty for topics whose
    #: output is free-form text (``sdiag``, ``scontrol show config``).
    columns: tuple[str, ...] = ()
    #: Longer guidance, returned only by ``slurm_describe``.
    detail: str = ""


TOPICS: dict[str, Topic] = {
    "queue": Topic(
        name="queue",
        summary="Pending and running jobs, with state and reason codes.",
        argv=("squeue", "--noheader", "-o", "%i|%P|%u|%T|%M|%D|%R"),
        filters={"user": "-u", "partition": "-p", "state": "-t"},
        columns=("jobid", "partition", "user", "state", "elapsed", "nodes", "reason"),
        # Reason definitions are quoted from
        # https://slurm.schedmd.com/job_reason_codes.html (checked 2026-09-26).
        detail=(
            "The reason code is the diagnostic payload. Per Slurm's reason-code "
            "reference: `Resources` — 'The resources requested by the job are not "
            "available (e.g., already used by other jobs).' `Priority` — 'One of more "
            "higher priority jobs exist for the partition associated with the job or "
            "for the advanced reservation.' `ReqNodeNotAvail` — 'Some node "
            "specifically required by the job is not currently available', which "
            "includes a DRAINED node you have not noticed. Resources means the job is "
            "waiting on capacity; Priority means it is waiting on other jobs ahead of "
            "it. Telling those two apart is a common queue question."
        ),
    ),
    "nodes": Topic(
        name="nodes",
        summary="Node and partition state, including drains and their reasons.",
        # -N: one line per node and partition (sinfo man page), so each drained
        # node gets its own reason line instead of being folded into a hostlist.
        argv=("sinfo", "--noheader", "-N", "-o", "%N|%P|%T|%C|%G|%E"),
        filters={"partition": "-p", "state": "-t"},
        columns=("node", "partition", "state", "cpus(A/I/O/T)", "gres", "reason"),
        detail=(
            "State is %T, sinfo's extended form, so a drain shows as drained "
            "(unavailable per administrator request) or draining (still running a "
            "job, taking no new ones), not the compact drain/drng. A default "
            "partition carries a trailing `*`. A drained node carries a free-text "
            "reason written by whatever drained it. An epilog validator, a human, and "
            "a kernel panic all look identical in the state column and different in "
            "the reason, so the reason is the field worth reading first."
        ),
    ),
    "accounting": Topic(
        name="accounting",
        summary="Completed job history from the accounting database.",
        argv=(
            "sacct",
            "-a",
            "-X",
            "--parsable2",
            "--noheader",
            "--format=JobID,Account,User,State,Elapsed,Timelimit,NNodes,ReqTRES",
        ),
        filters={"user": "-u", "starttime": "-S", "endtime": "-E", "state": "-s"},
        columns=(
            "jobid",
            "account",
            "user",
            "state",
            "elapsed",
            "timelimit",
            "nnodes",
            "reqtres",
        ),
        detail=(
            "Reads slurmdbd, not the controller. That distinction matters during an "
            "incident: if the accounting path is degraded this call can block or "
            "return stale rows while scheduling continues perfectly well. An empty "
            "or hanging sacct is evidence about slurmdbd, not about the scheduler.\n"
            "Without starttime, sacct starts at 00:00:00 today; a state filter changes "
            "that default (sacct man page). `-a` shows every user's jobs only when the "
            "server's account is root or PrivateData does not hide jobs; otherwise it "
            "shows that account's own jobs.\n"
            "Elapsed against Timelimit is the input to any backfill question."
        ),
    ),
    "priority": Topic(
        name="priority",
        summary="Priority breakdown per pending job, factor by factor.",
        # %r is the partition *name*; %P is the weighted partition *priority*
        # (sprio man page). v0.1.0 printed only %P and labelled it "partition".
        argv=("sprio", "--noheader", "-o", "%i|%r|%u|%Y|%A|%F|%J|%P|%Q"),
        filters={"user": "-u", "partition": "-p"},
        columns=(
            "jobid",
            "partition",
            "user",
            "priority",
            "age",
            "fairshare",
            "jobsize",
            "partition_prio",
            "qos",
        ),
        detail=(
            "Read the factors, not the total. A total tells you the order; the "
            "factors tell you which weight produced it, which is the only version "
            "that suggests a config change. All factor columns are weighted values."
        ),
    ),
    "fairshare": Topic(
        name="fairshare",
        summary="Fairshare tree: allocation, usage, and resulting share factor.",
        # An explicit --format pins the column order instead of relying on
        # sshare's default, which differs between the long and short forms
        # (src/sshare/process.c).
        argv=(
            "sshare",
            "--noheader",
            "--parsable2",
            "-a",
            "--format=Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare",
        ),
        filters={"user": "-u", "account": "-A"},
        columns=(
            "account",
            "user",
            "rawshares",
            "normshares",
            "rawusage",
            "effectvusage",
            "fairshare",
        ),
        detail=(
            "Usage decays on a half-life set by PriorityDecayHalfLife. An account "
            "that looks over-served may simply not have decayed yet, so read this "
            "alongside the half-life from the `config` topic rather than alone."
        ),
    ),
    "diagnostics": Topic(
        name="diagnostics",
        summary="Scheduler internals: cycle counts, backfill stats, RPC and agent queues.",
        argv=("sdiag",),
        filters={},
        detail=(
            "The first thing to read when scheduling feels slow. Backfill cycle time "
            "and 'last cycle' depth show whether the backfill scheduler is finishing "
            "its pass or being cut off by bf_max_time. A climbing DBD Agent queue "
            "means accounting is backing up. In one measured run (slurm-rca-bench "
            "scenario S01: Slurm 25.11.4 in Docker Compose, accounting database paused "
            "for about 15 minutes) that degraded job start latency but did not halt "
            "scheduling. That is one cluster and one scenario, not a general rule."
        ),
    ),
    "config": Topic(
        name="config",
        summary="Live controller configuration as slurmctld currently has it.",
        argv=("scontrol", "show", "config"),
        filters={},
        detail=(
            "The running config, which is not necessarily what is in slurm.conf on "
            "disk if someone edited without reconfiguring. Priority weights, "
            "SchedulerParameters, and the backfill knobs all live here."
        ),
    ),
}


def topic_names() -> list[str]:
    return sorted(TOPICS)


def check_filter_value(key: str, value: str) -> None:
    """Raise :class:`InvalidFilter` unless ``value`` is a plain filter value."""
    if not FILTER_VALUE.fullmatch(value):
        raise InvalidFilter(
            f"filter {key}={value!r} refused: values must start with a letter or digit "
            f"and contain only letters, digits and _ . , : @ + - (at most 128 chars)"
        )


def build_argv(topic_name: str, filters: dict[str, str] | None = None) -> list[str]:
    """Turn a topic plus filters into an argv list.

    Filters become separate argv elements, never interpolated into a string, so
    a value cannot grow into another argument. Values are checked here as well as
    in the guard: here so a caller gets a precise error, there so the check does
    not depend on every caller going through this function.
    """
    topic = TOPICS.get(topic_name)
    if topic is None:
        raise KeyError(f"unknown topic {topic_name!r}; have {', '.join(topic_names())}")

    argv = list(topic.argv)
    for key, value in (filters or {}).items():
        flag = topic.filters.get(key)
        if flag is None:
            raise KeyError(
                f"topic {topic_name!r} has no filter {key!r}; "
                f"accepts {', '.join(sorted(topic.filters)) or 'none'}"
            )
        check_filter_value(key, value)
        argv += [flag, value]
    return argv
