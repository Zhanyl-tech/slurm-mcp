"""Run a guarded command, or serve a fixture when there is no cluster.

Fixture mode is not a toy. A read-only MCP server is exactly the kind of thing
people want to try before pointing it at a production controller, and a server
that cannot be exercised without a Slurm cluster will not be reviewed by
anyone. `--fixtures` makes the whole surface runnable on a laptop.

Every response says which mode produced it. A fixture must never be mistakable
for a live read.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .guard import Denied, guard
from .topics import TOPICS, build_argv

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
#: Slurm calls block indefinitely when the accounting path is degraded, which
#: is a real incident shape. Never wait forever on behalf of an agent.
TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class Result:
    topic: str
    argv: list[str]
    stdout: str
    #: "live" or "fixture". Printed on every response.
    source: str
    exit_code: int = 0
    error: str | None = None

    def render(self) -> str:
        head = f"[{self.source}] $ {' '.join(self.argv)}"
        if self.error:
            return f"{head}\n\n{self.error}"
        body = self.stdout.strip() or "(no rows)"
        return f"{head}\n\n{body}"


def slurm_available() -> bool:
    return shutil.which("sinfo") is not None


def run_topic(
    topic: str, filters: dict[str, str] | None = None, *, use_fixtures: bool = False
) -> Result:
    argv = build_argv(topic, filters)
    try:
        guard(argv)
    except Denied as exc:
        # Should be unreachable: topics are a closed vocabulary. Kept because
        # "unreachable" is a claim, and the guard is the thing that makes it true.
        return Result(topic, argv, "", "denied", 1, f"denied: {exc}")

    if use_fixtures:
        path = FIXTURES / f"{topic}.txt"
        if not path.exists():
            return Result(topic, argv, "", "fixture", 1, f"no fixture for topic {topic!r}")
        return Result(topic, argv, path.read_text(encoding="utf-8"), "fixture")

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, shell=False
        )
    except FileNotFoundError:
        return Result(
            topic,
            argv,
            "",
            "live",
            127,
            f"{argv[0]} not found. Run with --fixtures to exercise the surface "
            f"without a Slurm cluster.",
        )
    except subprocess.TimeoutExpired:
        return Result(
            topic,
            argv,
            "",
            "live",
            124,
            f"{argv[0]} did not return within {TIMEOUT_SECONDS:.0f}s. On a healthy "
            f"cluster this is instant; a hang here is itself evidence, usually that "
            f"the accounting path is degraded.",
        )
    return Result(
        topic,
        argv,
        proc.stdout,
        "live",
        proc.returncode,
        proc.stderr.strip() or None if proc.returncode else None,
    )


def overview(*, use_fixtures: bool = False) -> str:
    """The cheap snapshot most sessions open with.

    Three topics, no filters, one call — instead of an agent discovering it
    needs nodes+queue+diagnostics one round trip at a time.
    """
    parts = []
    for topic in ("nodes", "queue", "diagnostics"):
        r = run_topic(topic, use_fixtures=use_fixtures)
        parts.append(f"### {topic} — {TOPICS[topic].summary}\n{r.render()}")
    return "\n\n".join(parts)
