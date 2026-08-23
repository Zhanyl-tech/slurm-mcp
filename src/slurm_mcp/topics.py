"""Progressive disclosure: three tools instead of eight, detail on request.

The obvious MCP server for Slurm exposes one tool per binary — `sinfo`,
`squeue`, `sacct`, `sdiag`, `sprio`, `sshare`, `scontrol`, `sacctmgr` — each
with a schema describing its flags. That is the design this repo argues
against, for a measurable reason: every one of those schemas is in the model's
context on every single turn, whether the question is about the queue or not.
Slurm's flag surface is enormous, and most of it is irrelevant to any given
question.

So the server exposes three tools:

* ``slurm_overview`` — one cheap snapshot answering "what is the cluster doing",
  the question most sessions open with.
* ``slurm_query`` — a topic plus optional filters. Topics are a short closed
  vocabulary, not a command line.
* ``slurm_describe`` — the flags and output shape for *one* topic, fetched only
  when the agent has decided it needs them.

The agent pays for detail when it asks for it. Nothing here prevents a client
from being wasteful, but the default path is cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Topic:
    """One question an operator asks, and the read that answers it."""

    name: str
    summary: str
    #: Base command. Filters are appended by the caller, never interpolated.
    argv: tuple[str, ...]
    #: Filters this topic understands, mapped to the flag they become.
    filters: dict[str, str] = field(default_factory=dict)
    #: Longer guidance, returned only by ``slurm_describe``.
    detail: str = ""


TOPICS: dict[str, Topic] = {
    "queue": Topic(
        name="queue",
        summary="Pending and running jobs, with state and reason codes.",
        argv=("squeue", "--noheader", "-o", "%i|%P|%u|%T|%M|%D|%R"),
        filters={"user": "-u", "partition": "-p", "state": "-t"},
        detail=(
            "Columns: jobid|partition|user|state|elapsed|nodes|reason.\n"
            "The reason code is the diagnostic payload. `Resources` means the job "
            "would fit but the cluster is full; `Priority` means something ahead of "
            "it is holding the reservation; `ReqNodeNotAvail` usually means a drain "
            "you have not noticed. Distinguishing the first two is the single most "
            "common queue question, and they mean opposite things."
        ),
    ),
    "nodes": Topic(
        name="nodes",
        summary="Node and partition state, including drains and their reasons.",
        argv=("sinfo", "--noheader", "-o", "%n|%P|%T|%C|%G|%E"),
        filters={"partition": "-p", "state": "-t"},
        detail=(
            "Columns: node|partition|state|cpus(A/I/O/T)|gres|reason.\n"
            "A node in `drain` or `drng` carries a free-text reason written by "
            "whatever drained it. An epilog validator, a human, and a kernel panic "
            "all look identical in the state column and different in the reason, so "
            "the reason is the field worth reading first."
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
        detail=(
            "Reads slurmdbd, not the controller. That distinction matters during an "
            "incident: if the accounting path is degraded this call can block or "
            "return stale rows while scheduling continues perfectly well. An empty "
            "or hanging sacct is evidence about slurmdbd, not about the scheduler.\n"
            "Elapsed against Timelimit is the input to any backfill question."
        ),
    ),
    "priority": Topic(
        name="priority",
        summary="Priority breakdown per pending job, factor by factor.",
        argv=("sprio", "--noheader", "-o", "%i|%u|%Y|%A|%F|%J|%P|%Q"),
        filters={"user": "-u", "partition": "-p"},
        detail=(
            "Columns: jobid|user|priority|age|fairshare|jobsize|partition|qos.\n"
            "Read the factors, not the total. A total tells you the order; the "
            "factors tell you which weight produced it, which is the only version "
            "that suggests a config change."
        ),
    ),
    "fairshare": Topic(
        name="fairshare",
        summary="Fairshare tree: allocation, usage, and resulting share factor.",
        argv=("sshare", "--noheader", "--parsable2", "-a"),
        filters={"user": "-u", "account": "-A"},
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
            "means accounting is backing up — which degrades job start latency but, "
            "measured, does not halt scheduling."
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


def build_argv(topic_name: str, filters: dict[str, str] | None = None) -> list[str]:
    """Turn a topic plus filters into an argv list.

    Filters become separate argv elements, never interpolated into a string, so
    a value cannot grow into another argument.
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
        argv += [flag, str(value)]
    return argv
