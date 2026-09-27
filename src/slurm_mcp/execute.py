"""Run a guarded command, or serve a fixture when there is no cluster.

Fixture mode is not a toy. A read-only MCP server is exactly the kind of thing
people want to try before pointing it at a production controller, and a server
that cannot be exercised without a Slurm cluster will not be reviewed by
anyone. `--fixtures` makes the whole surface runnable on a laptop. The fixtures
are hand-written illustrative samples shaped like each topic's output, not
recordings of a real cluster (see ``fixtures/README.md``).

Every response says which mode produced it. A fixture must never be mistakable
for a live read.

Live reads go through a :class:`Gate`, because a read-only server can still hurt
a struggling controller. The squeue man page is explicit: "Do not run squeue or
other Slurm client commands that send remote procedure calls to slurmctld from
loops in shell scripts or other programs" — and an agent retrying
``slurm_overview`` is such a loop. The gate caches each distinct argv briefly,
runs one subprocess for concurrent identical calls while that cache is on,
bounds how many Slurm commands run at once, and caps how many start per minute.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import TextIO

from .guard import Denied, guard
from .topics import TOPICS, Topic, build_argv

log = logging.getLogger("slurm_mcp")

#: Slurm calls block indefinitely when the accounting path is degraded, which
#: is a real incident shape. Never wait forever on behalf of an agent.
TIMEOUT_SECONDS = 20.0

#: Filters that fixture mode can apply itself, as an exact match against the
#: column of the same name. Other filters (states, times) have Slurm semantics —
#: compact versus long state names, relative times — that a string match on
#: hand-written rows would only imitate, so fixture mode refuses them.
FIXTURE_FILTERABLE = frozenset({"user", "partition", "account"})


@dataclass(frozen=True)
class Limits:
    """How hard this server may lean on slurmctld and on the agent's context.

    The defaults are judgement calls, not tuned against a real controller. They
    apply per server process: N clients each launching their own stdio server
    get N times the budget.
    """

    #: Seconds a live answer is reused for the identical argv. 0 disables.
    cache_ttl: float = 10.0
    #: Slurm commands allowed to run at the same time.
    max_concurrent: int = 2
    #: Slurm commands allowed to *start* in any 60-second window. Cached answers
    #: do not count. 0 disables.
    max_calls_per_minute: int = 30
    #: Rows returned for tabular topics before truncating with a trailer. At
    #: least 1: there is no "unlimited" setting.
    max_rows: int = 200
    #: Characters returned for free-text topics (sdiag, scontrol show config).
    max_chars: int = 50_000

    def __post_init__(self) -> None:
        # A cap of 0 keeps nothing, so a non-empty queue would render as
        # "(no rows)", which is the ambiguity a failed read used to have. Unlike
        # the cache and the rate limit, this bound has no "off" value.
        if self.max_rows < 1 or self.max_chars < 1:
            raise ValueError(
                f"max_rows and max_chars must be at least 1, got {self.max_rows} "
                f"and {self.max_chars}"
            )


@dataclass(frozen=True)
class Outcome:
    """What one Slurm subprocess did, before any rendering."""

    stdout: str
    stderr: str = ""
    exit_code: int = 0
    error: str | None = None


@dataclass(frozen=True)
class Result:
    topic: str
    argv: list[str]
    stdout: str
    #: "live", "fixture" or "denied". Printed on every response.
    source: str
    exit_code: int = 0
    error: str | None = None
    stderr: str = ""
    #: Seconds since this answer was fetched, when it came from the cache.
    cached_age: float | None = None
    #: Lines appended after the body: how fixture filters were applied, then
    #: any truncation trailer. Both are kept when both apply.
    note: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def render(self) -> str:
        label = self.source
        if self.cached_age is not None:
            label += f", cached {self.cached_age:.0f}s ago"
        head = f"[{label}] $ {shlex.join(self.argv)}"
        if self.error is not None:
            parts = [head, f"error: {self.error}"]
            if self.stdout.strip():
                parts.append(f"stdout before the failure:\n{self.stdout.strip()}")
        else:
            parts = [head, self.stdout.strip() or "(no rows)"]
        if self.note:
            parts.append(self.note)
        return "\n\n".join(parts)


def slurm_available() -> bool:
    return shutil.which("sinfo") is not None


def run_subprocess(argv: list[str]) -> Outcome:
    """Run one already-guarded argv. Never raises for anything the OS or Slurm does."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            shell=False,
            # Drain reasons and job names are free text written by admins and
            # scripts. One invalid UTF-8 byte must not take down the snapshot.
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return Outcome(
            "",
            exit_code=127,
            error=f"{argv[0]} not found. Run with --fixtures to exercise the surface "
            f"without a Slurm cluster.",
        )
    except subprocess.TimeoutExpired:
        return Outcome(
            "",
            exit_code=124,
            error=f"{argv[0]} did not return within {TIMEOUT_SECONDS:.0f}s. On a healthy "
            f"cluster this is instant; a hang here is itself evidence, usually that "
            f"the accounting path is degraded.",
        )
    except (OSError, ValueError) as exc:
        # PermissionError, a NUL byte in an argument, and the like.
        return Outcome("", exit_code=126, error=f"could not run {argv[0]}: {exc}")

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        return Outcome(
            proc.stdout,
            stderr,
            proc.returncode,
            f"{argv[0]} exited {proc.returncode}; stderr: {stderr or '(empty)'}. "
            f"The read failed; this is not an empty result.",
        )
    return Outcome(proc.stdout, proc.stderr.strip(), 0, None)


@dataclass
class _Flight:
    """Callers currently asking for one argv; the lock makes them take turns."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    waiters: int = 0


class Gate:
    """Cache, single-flight, a concurrency bound and a rate limit for live reads.

    Thread-safe, because the MCP server runs each tool call in a worker thread so
    the event loop stays free for other requests and cancellation while Slurm
    answers.
    """

    def __init__(
        self, limits: Limits | None = None, *, clock: Callable[[], float] = time.monotonic
    ):
        self.limits = limits or Limits()
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, ...], tuple[float, Outcome]] = {}
        # Removed when the last waiter leaves, so this is bounded by the number
        # of calls in flight, not by history.
        self._inflight: dict[tuple[str, ...], _Flight] = {}
        self._slots = threading.BoundedSemaphore(max(1, self.limits.max_concurrent))
        self._starts: deque[float] = deque()
        #: Subprocesses actually launched. Tests assert on it.
        self.launched = 0

    def _cached(self, key: tuple[str, ...], now: float) -> tuple[Outcome, float] | None:
        with self._lock:
            hit = self._cache.get(key)
        if hit is None or self.limits.cache_ttl <= 0:
            return None
        age = now - hit[0]
        return (hit[1], age) if age < self.limits.cache_ttl else None

    def _admit(self, now: float) -> str | None:
        """Record a start at ``now``, or say why it is refused."""
        cap = self.limits.max_calls_per_minute
        with self._lock:
            while self._starts and now - self._starts[0] >= 60.0:
                self._starts.popleft()
            if cap > 0 and len(self._starts) >= cap:
                retry = 60.0 - (now - self._starts[0])
                return (
                    f"rate limited: {cap} Slurm commands already started in the last "
                    f"60s. Retry in {retry:.0f}s, or reuse the last answer. The squeue "
                    f"man page warns that loops of client commands can degrade slurmctld."
                )
            self._starts.append(now)
            return None

    def fetch(
        self, argv: list[str], run: Callable[[list[str]], Outcome] = run_subprocess
    ) -> tuple[Outcome, float | None]:
        """Return an outcome for ``argv`` and, if it came from the cache, its age."""
        key = tuple(argv)
        with self._lock:
            flight = self._inflight.setdefault(key, _Flight())
            flight.waiters += 1
        try:
            # Identical concurrent calls queue here and find the first one's
            # answer in the cache instead of each starting a subprocess. That
            # sharing *is* the cache: with cache_ttl 0 they still queue here,
            # one at a time, and each runs its own subprocess.
            with flight.lock:
                hit = self._cached(key, self._clock())
                if hit is not None:
                    self._log(argv, hit[0], 0.0, cached=True)
                    return hit
                admitted = self._clock()
                refusal = self._admit(admitted)
                if refusal is not None:
                    return Outcome("", exit_code=429, error=refusal), None
                if not self._slots.acquire(timeout=TIMEOUT_SECONDS):
                    with self._lock, contextlib.suppress(ValueError):
                        # Nothing started, so give the budget back (unless the
                        # entry already aged out of the window).
                        self._starts.remove(admitted)
                    return (
                        Outcome(
                            "",
                            exit_code=503,
                            error=f"busy: waited {TIMEOUT_SECONDS:.0f}s for one of "
                            f"{self.limits.max_concurrent} Slurm command slots; not adding "
                            f"another while slurmctld is this slow.",
                        ),
                        None,
                    )
                started = self._clock()
                try:
                    with self._lock:
                        self.launched += 1
                    outcome = run(argv)
                finally:
                    self._slots.release()
                done = self._clock()
                self._log(argv, outcome, done - started, cached=False)
                with self._lock:
                    ttl = self.limits.cache_ttl
                    for k in [k for k, (t, _) in self._cache.items() if done - t >= ttl]:
                        del self._cache[k]
                    if ttl > 0:
                        self._cache[key] = (done, outcome)
                return outcome, None
        finally:
            with self._lock:
                flight.waiters -= 1
                if flight.waiters == 0:
                    del self._inflight[key]

    @staticmethod
    def _log(argv: list[str], outcome: Outcome, seconds: float, *, cached: bool) -> None:
        # One structured line per live read, for audit, including reads the
        # cache answered ("cached": true, when no Slurm command ran). Calls the
        # gate refuses never reach here. stdio servers may write to stderr
        # freely; `serve` attaches audit_handler().
        log.info(
            json.dumps(
                {
                    "argv": argv,
                    "exit_code": outcome.exit_code,
                    "seconds": round(seconds, 3),
                    "cached": cached,
                }
            )
        )


def audit_handler(stream: TextIO) -> logging.Handler:
    """The handler ``serve`` attaches: each audit line is one bare JSON object.

    No prefix on the line, so a log shipper or ``jq`` can parse every line
    as it stands.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


_gate = Gate()


def gate() -> Gate:
    return _gate


def configure(limits: Limits) -> Gate:
    """Replace the process-wide gate (and so drop its cache)."""
    global _gate
    _gate = Gate(limits)
    return _gate


def read_fixture(topic: str) -> str | None:
    """Fixtures ship inside the package, so they work from a wheel, not only a checkout."""
    path = resources.files("slurm_mcp").joinpath("fixtures", f"{topic}.txt")
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _fixture_filters(topic: Topic) -> list[str]:
    """The topic's filters that fixture mode applies itself (the rest it refuses)."""
    return sorted(k for k in topic.filters if k in FIXTURE_FILTERABLE and k in topic.columns)


def _filter_fixture(topic: Topic, text: str, filters: dict[str, str]) -> tuple[str, str | None]:
    """Apply what fixture mode honestly can; raise ValueError for the rest."""
    if not filters:
        return text, None
    refused = sorted(k for k in filters if k not in FIXTURE_FILTERABLE or k not in topic.columns)
    if refused:
        raise ValueError(
            f"fixture mode does not apply the {', '.join(refused)} filter to the "
            f"hand-written sample data (Slurm's own matching rules would only be "
            f"imitated). Drop it, or run against a live cluster."
        )
    rows = [line for line in text.splitlines() if line.strip()]
    for key, value in filters.items():
        col = topic.columns.index(key)
        wanted = set(value.split(","))
        rows = [
            r for r in rows if len(r.split("|")) > col and r.split("|")[col].rstrip("*") in wanted
        ]
    note = (
        f"[fixture] {', '.join(f'{k}={v}' for k, v in filters.items())} was applied by "
        f"this server as an exact match on the sample rows, not by {topic.argv[0]}."
    )
    return "\n".join(rows), note


def _cap(
    topic: Topic, stdout: str, limits: Limits, *, narrow_with: list[str] | None = None
) -> tuple[str, str | None]:
    """Bound what one response can put into the agent's context.

    ``narrow_with`` is the filters the trailer suggests; by default all of the
    topic's. Fixture mode passes only the ones it applies, so the hint never
    points at a filter the next call would refuse.
    """
    if topic.columns:
        rows = [line for line in stdout.splitlines() if line.strip()]
        if len(rows) <= limits.max_rows:
            return stdout, None
        hint = sorted(topic.filters) if narrow_with is None else narrow_with
        narrow = ", ".join(hint) or "none"
        return "\n".join(rows[: limits.max_rows]), (
            f"[truncated: {limits.max_rows} of {len(rows)} rows shown; narrow with "
            f"filters: {narrow}]"
        )
    if len(stdout) <= limits.max_chars:
        return stdout, None
    return stdout[: limits.max_chars], (
        f"[truncated: {limits.max_chars} of {len(stdout)} characters shown]"
    )


def run_topic(
    topic: str,
    filters: dict[str, str] | None = None,
    *,
    use_fixtures: bool = False,
    via: Gate | None = None,
) -> Result:
    """Read one topic.

    Raises ``KeyError`` or ``ValueError`` for a request that is malformed (an
    unknown topic or filter, an unsafe filter value, a filter fixture mode cannot
    apply). Everything that happens after the request is valid — a denial, a
    missing binary, a timeout, a non-zero exit — comes back as a :class:`Result`.
    """
    filters = filters or {}
    argv = build_argv(topic, filters)
    spec = TOPICS[topic]
    g = via or _gate
    try:
        guard(argv)
    except Denied as exc:
        # Should be unreachable: topics are a closed vocabulary. Kept because
        # "unreachable" is a claim, and the guard is the thing that makes it true.
        return Result(topic, argv, "", "denied", 1, f"denied: {exc}")

    if use_fixtures:
        text = read_fixture(topic)
        if text is None:
            return Result(topic, argv, "", "fixture", 1, f"no fixture for topic {topic!r}")
        text, filter_note = _filter_fixture(spec, text, filters)
        text, cap_note = _cap(spec, text, g.limits, narrow_with=_fixture_filters(spec))
        # Both lines, when both apply: truncating the rows does not make the
        # header's `-u bob` any less a claim that Slurm never saw.
        notes = [n for n in (filter_note, cap_note) if n]
        return Result(topic, argv, text, "fixture", note="\n".join(notes) or None)

    outcome, age = g.fetch(argv)
    stdout, cap_note = _cap(spec, outcome.stdout, g.limits)
    return Result(
        topic,
        argv,
        stdout,
        "live",
        outcome.exit_code,
        outcome.error,
        outcome.stderr,
        cached_age=age,
        note=cap_note,
    )


OVERVIEW_TOPICS = ("nodes", "queue", "diagnostics")


def overview(*, use_fixtures: bool = False, via: Gate | None = None) -> list[Result]:
    """The cheap snapshot most sessions open with.

    Three topics, no filters, one call — instead of an agent discovering it
    needs nodes+queue+diagnostics one round trip at a time. They run one after
    another on purpose: three concurrent RPCs would be harder on a struggling
    controller than three sequential ones.
    """
    return [run_topic(t, use_fixtures=use_fixtures, via=via) for t in OVERVIEW_TOPICS]


def render_overview(results: list[Result]) -> str:
    return "\n\n".join(f"### {r.topic} — {TOPICS[r.topic].summary}\n{r.render()}" for r in results)
