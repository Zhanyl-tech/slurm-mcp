"""The live path, the gate in front of slurmctld, and fixture mode.

Live-path tests use fake Slurm binaries on PATH (see conftest.fakebin): real
subprocesses and real PATH lookup, no cluster. Nothing here claims anything
about how a real slurmctld responds.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from slurm_mcp import execute
from slurm_mcp.execute import (
    Gate,
    Limits,
    Outcome,
    Result,
    audit_handler,
    configure,
    overview,
    read_fixture,
    render_overview,
    run_subprocess,
    run_topic,
)
from slurm_mcp.topics import TOPICS, Topic, topic_names

#: See conftest.fakebin.
FakeBin = Callable[[str, str], Path]

README = Path(__file__).resolve().parent.parent / "README.md"


# --- live path, through fake binaries -------------------------------------


def test_live_read_returns_stdout_and_says_live(fakebin: FakeBin) -> None:
    fakebin("squeue", "echo '1|gpu|alice|RUNNING|0:01|1|n1'")
    r = run_topic("queue")
    assert not r.failed
    assert r.render().startswith("[live] $ squeue --noheader -o '%i|%P|%u|%T|%M|%D|%R'")
    assert "1|gpu|alice|RUNNING" in r.render()


def test_a_silent_failure_is_not_rendered_as_an_empty_queue(fakebin: FakeBin) -> None:
    """exit 1 with no output used to render exactly like a healthy empty queue."""
    fakebin("squeue", "exit 1")
    r = run_topic("queue")
    assert r.failed and r.exit_code == 1
    out = r.render()
    assert "(no rows)" not in out
    assert "squeue exited 1; stderr: (empty)" in out
    assert "not an empty result" in out


def test_a_genuinely_empty_queue_is_no_rows(fakebin: FakeBin) -> None:
    fakebin("squeue", "exit 0")
    r = run_topic("queue")
    assert not r.failed
    assert r.render().endswith("(no rows)")


def test_a_failure_keeps_both_stderr_and_stdout(fakebin: FakeBin) -> None:
    fakebin(
        "squeue",
        "echo '1|gpu|alice|RUNNING|0:01|1|n1'; echo 'squeue: error: partial' >&2; exit 1",
    )
    out = run_topic("queue").render()
    assert "stderr: squeue: error: partial" in out
    assert "1|gpu|alice|RUNNING" in out


def test_invalid_utf8_in_a_drain_reason_does_not_take_down_the_overview(fakebin: FakeBin) -> None:
    fakebin("sinfo", r"printf 'gpu-09|gpu|drained|0/0/64/64|gpu:8|ECC \351rror\n'")
    fakebin("squeue", "exit 0")
    fakebin("sdiag", "echo 'Server thread count: 3'")
    results = overview()
    assert [r.topic for r in results] == ["nodes", "queue", "diagnostics"]
    assert not any(r.failed for r in results)
    assert "ECC �rror" in results[0].stdout


def test_a_missing_binary_is_reported(fakebin: FakeBin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(Path(fakebin("unrelated", "exit 0")).parent))
    r = run_topic("queue")
    assert r.failed and r.exit_code == 127
    assert "squeue not found" in r.render()


def test_the_timeout_is_twenty_seconds_and_the_readme_says_so() -> None:
    assert execute.TIMEOUT_SECONDS == 20.0
    assert "time out at 20s" in README.read_text(encoding="utf-8")


def test_a_hung_binary_times_out(fakebin: FakeBin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execute, "TIMEOUT_SECONDS", 0.5)
    fakebin("sacct", "exec sleep 30")
    started = time.monotonic()
    r = run_topic("accounting")
    assert time.monotonic() - started < 10
    assert r.failed and r.exit_code == 124
    assert "hang here is itself evidence" in r.render()


def test_os_errors_become_results_not_exceptions(fakebin: FakeBin) -> None:
    path = fakebin("squeue", "exit 0")
    path.chmod(0o644)  # on PATH, not executable
    r = run_subprocess(["squeue"])
    assert r.error is not None and r.exit_code in (126, 127)
    nul = run_subprocess(["squeue", "-u", "a\x00b"])
    assert nul.error is not None and nul.exit_code == 126


def test_a_denied_argv_never_reaches_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail safe: if the vocabulary ever produced a mutating argv, nothing runs."""

    def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run was called for a denied command")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    monkeypatch.setitem(
        TOPICS, "queue", Topic("queue", "x", ("scontrol", "update", "NodeName=ALL", "State=DRAIN"))
    )
    r = run_topic("queue")
    assert r.source == "denied" and r.failed
    assert r.render().startswith("[denied]")


# --- the gate: cache, single-flight, concurrency, rate ---------------------


def test_identical_calls_inside_the_ttl_invoke_the_binary_once(
    fakebin: FakeBin, tmp_path: Path
) -> None:
    counter = tmp_path / "count"
    fakebin("squeue", f"echo x >> {counter}; echo '1|gpu|alice|RUNNING|0:01|1|n1'")
    first = run_topic("queue")
    for _ in range(9):
        again = run_topic("queue")
    assert counter.read_text().count("x") == 1
    assert first.cached_age is None
    assert again.cached_age is not None
    assert again.render().startswith("[live, cached 0s ago]")


def test_different_filters_are_different_cache_entries(fakebin: FakeBin, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    fakebin("squeue", f"echo x >> {counter}")
    run_topic("queue", {"user": "alice"})
    run_topic("queue", {"user": "bob"})
    run_topic("queue", {"user": "alice"})
    assert counter.read_text().count("x") == 2


def test_the_cache_expires(fakebin: FakeBin, tmp_path: Path) -> None:
    now = [100.0]
    g = Gate(Limits(cache_ttl=10), clock=lambda: now[0])
    calls: list[list[str]] = []

    def run(argv: list[str]) -> Outcome:
        calls.append(argv)
        return Outcome("ok")

    g.fetch(["sdiag"], run)
    now[0] += 9.9
    assert g.fetch(["sdiag"], run)[1] == pytest.approx(9.9)
    now[0] += 0.2
    assert g.fetch(["sdiag"], run)[1] is None
    assert len(calls) == 2


def test_cache_ttl_zero_disables_the_cache() -> None:
    g = Gate(Limits(cache_ttl=0))
    for _ in range(3):
        g.fetch(["sdiag"], lambda argv: Outcome("ok"))
    assert g.launched == 3


def test_concurrent_identical_calls_share_one_subprocess() -> None:
    g = Gate(Limits(max_concurrent=4))

    def slow(argv: list[str]) -> Outcome:
        time.sleep(0.3)
        return Outcome("ok")

    threads = [threading.Thread(target=g.fetch, args=(["sdiag"], slow)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert g.launched == 1


def test_without_the_cache_identical_calls_take_turns_and_each_runs() -> None:
    """Single-flight is the cache: with --cache-ttl 0 nothing is shared.

    The README says so, because "concurrent identical calls share one
    subprocess" without that condition was false for a documented setting.
    """
    g = Gate(Limits(max_concurrent=4, cache_ttl=0))
    lock = threading.Lock()
    running = [0]
    peak = [0]

    def slow(argv: list[str]) -> Outcome:
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return Outcome("ok")

    threads = [threading.Thread(target=g.fetch, args=(["sdiag"], slow)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert g.launched == 5
    assert peak[0] == 1  # identical argv still queue on one flight lock
    assert "while the cache is on" in README.read_text(encoding="utf-8")


def test_no_more_than_max_concurrent_slurm_commands_run_at_once() -> None:
    g = Gate(Limits(max_concurrent=2, cache_ttl=0))
    lock = threading.Lock()
    running = [0]
    peak = [0]

    def slow(argv: list[str]) -> Outcome:
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.15)
        with lock:
            running[0] -= 1
        return Outcome("ok")

    threads = [
        threading.Thread(target=g.fetch, args=([f"q{i}"], slow)) for i in range(6)
    ]  # distinct argv, so single-flight does not collapse them
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert g.launched == 6
    assert peak[0] == 2


def test_the_rate_limit_refuses_with_a_retry_hint_and_recovers() -> None:
    now = [0.0]
    g = Gate(Limits(max_calls_per_minute=3, cache_ttl=0), clock=lambda: now[0])
    ok = [g.fetch([f"q{i}"], lambda argv: Outcome("ok"))[0] for i in range(3)]
    assert all(o.error is None for o in ok)
    refused, _ = g.fetch(["q3"], lambda argv: Outcome("ok"))
    assert refused.exit_code == 429
    assert refused.error is not None and "rate limited" in refused.error
    assert "Retry in 60s" in refused.error
    assert g.launched == 3
    now[0] = 61.0
    assert g.fetch(["q4"], lambda argv: Outcome("ok"))[0].error is None


def test_cached_answers_do_not_spend_the_rate_budget() -> None:
    g = Gate(Limits(max_calls_per_minute=1, cache_ttl=60))
    for _ in range(5):
        outcome, _ = g.fetch(["sdiag"], lambda argv: Outcome("ok"))
        assert outcome.error is None
    assert g.launched == 1


def test_inflight_bookkeeping_does_not_grow_with_history() -> None:
    g = Gate(Limits(cache_ttl=0))
    for i in range(50):
        g.fetch([f"q{i}"], lambda argv: Outcome("ok"))
    assert g._inflight == {}
    assert g._cache == {}


def test_each_live_read_is_logged_as_one_structured_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    g = Gate()
    with caplog.at_level("INFO", logger="slurm_mcp"):
        g.fetch(["sdiag"], lambda argv: Outcome("ok"))
        g.fetch(["sdiag"], lambda argv: Outcome("ok"))
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert '"argv": ["sdiag"]' in lines[0] and '"cached": false' in lines[0]
    assert '"cached": true' in lines[1]


def test_the_audit_handler_writes_bare_json_lines() -> None:
    """The formatter `serve` installs: every line must parse as JSON on its own.

    tests/test_transport.py checks the same thing on a real server's stderr;
    this one runs without the MCP SDK.
    """
    stream = io.StringIO()
    handler = audit_handler(stream)
    execute.log.addHandler(handler)
    previous = execute.log.level
    execute.log.setLevel(logging.INFO)
    try:
        g = Gate()
        g.fetch(["sdiag"], lambda argv: Outcome("ok"))
        g.fetch(["sdiag"], lambda argv: Outcome("ok"))
    finally:
        execute.log.removeHandler(handler)
        execute.log.setLevel(previous)
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [r["cached"] for r in records] == [False, True]
    assert set(records[0]) == {"argv", "exit_code", "seconds", "cached"}


# --- output bounds ---------------------------------------------------------


def test_tabular_output_is_capped_with_an_explicit_trailer(fakebin: FakeBin) -> None:
    fakebin(
        "squeue",
        'i=0; while [ $i -lt 10000 ]; do echo "$i|gpu|u|PENDING|0:00|1|x"; i=$((i+1)); done',
    )
    r = run_topic("queue")
    assert len(r.stdout.splitlines()) == 200
    assert (
        "[truncated: 200 of 10000 rows shown; narrow with filters: partition, state, user]"
        in r.render()
    )


def test_the_row_cap_is_configurable(fakebin: FakeBin) -> None:
    configure(Limits(max_rows=5))
    fakebin("squeue", 'for i in 1 2 3 4 5 6 7; do echo "$i|gpu|u|PENDING|0:00|1|x"; done')
    assert "[truncated: 5 of 7 rows shown" in run_topic("queue").render()


@pytest.mark.parametrize("bad", [{"max_rows": 0}, {"max_rows": -1}, {"max_chars": 0}])
def test_a_cap_that_keeps_nothing_is_refused(bad: dict[str, int]) -> None:
    """A zero row cap rendered a six-job queue as "(no rows)"."""
    with pytest.raises(ValueError, match="at least 1"):
        Limits(**bad)


def test_free_text_output_is_capped_by_characters(fakebin: FakeBin) -> None:
    configure(Limits(max_chars=100))
    fakebin(
        "sdiag", "i=0; while [ $i -lt 100 ]; do echo 'Server thread count: 3'; i=$((i+1)); done"
    )
    r = run_topic("diagnostics")
    assert len(r.stdout) == 100
    assert "[truncated: 100 of" in r.render()


# --- fixture mode ----------------------------------------------------------


def test_fixtures_ship_inside_the_package() -> None:
    """Read through importlib.resources, so a wheel install finds them."""
    for name in topic_names():
        text = read_fixture(name)
        assert text and text.strip(), name
    assert read_fixture("nope") is None


def test_fixture_mode_serves_every_topic() -> None:
    for name in topic_names():
        r = run_topic(name, use_fixtures=True)
        assert r.source == "fixture"
        assert r.stdout.strip(), name


def test_responses_always_state_their_source() -> None:
    """A fixture must never be mistakable for a live read."""
    assert run_topic("queue", use_fixtures=True).render().startswith("[fixture]")


def test_fixture_filters_are_applied_not_just_echoed() -> None:
    """v0.1.0 printed `-u bob` in the header and returned every user's rows."""
    r = run_topic("queue", {"user": "bob"}, use_fixtures=True)
    rows = r.stdout.splitlines()
    assert rows and all(row.split("|")[2] == "bob" for row in rows)
    assert "applied by this server" in r.render()


def test_fixture_partition_filter_ignores_the_default_partition_star() -> None:
    rows = run_topic("nodes", {"partition": "cpu"}, use_fixtures=True).stdout.splitlines()
    assert rows and all(row.split("|")[1] == "cpu" for row in rows)


def test_fixture_mode_refuses_filters_it_cannot_apply_honestly() -> None:
    with pytest.raises(ValueError, match="does not apply the state filter"):
        run_topic("queue", {"state": "PENDING"}, use_fixtures=True)


def test_a_truncated_fixture_read_still_says_who_applied_the_filter() -> None:
    """The header shows `-p gpu`, which sinfo never saw. Truncation used to
    replace the line saying so, because Result kept only one note."""
    configure(Limits(max_rows=3))
    out = run_topic("nodes", {"partition": "gpu"}, use_fixtures=True).render()
    assert out.startswith("[fixture] $ sinfo")
    note = "[fixture] partition=gpu was applied by this server as an exact match"
    trailer = "[truncated: 3 of 4 rows shown; narrow with filters: partition]"
    assert note in out and trailer in out
    assert out.index(note) < out.index(trailer)  # the trailer stays last
    assert out.endswith(trailer)


def test_the_fixture_truncation_hint_names_only_filters_fixture_mode_applies() -> None:
    """Suggesting `state` would send the agent to a filter fixture mode refuses."""
    configure(Limits(max_rows=1))
    for name in ("queue", "nodes", "accounting", "priority", "fairshare"):
        out = run_topic(name, use_fixtures=True).render()
        hint = out.rsplit("narrow with filters: ", 1)[1].rstrip("]")
        for key in hint.split(", "):
            if key != "none":
                run_topic(name, {key: "x"}, use_fixtures=True)  # raises if refused
        assert "state" not in hint and "starttime" not in hint, (name, hint)


def test_overview_renders_three_sections_from_fixtures() -> None:
    out = render_overview(overview(use_fixtures=True))
    for expected in ("nodes", "queue", "diagnostics"):
        assert f"### {expected}" in out


def test_result_render_of_an_error_does_not_say_no_rows() -> None:
    r = Result("queue", ["squeue"], "", "live", 2, "squeue exited 2; stderr: (empty).")
    assert "(no rows)" not in r.render()


def test_a_saturated_gate_refuses_instead_of_queueing_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every slot held past the wait limit, a new read is refused as busy."""
    monkeypatch.setattr(execute, "TIMEOUT_SECONDS", 0.2)
    g = Gate(Limits(max_concurrent=1, cache_ttl=0))
    release = threading.Event()

    def held(argv: list[str]) -> Outcome:
        release.wait(5)
        return Outcome("ok")

    holder = threading.Thread(target=g.fetch, args=(["q-held"], held))
    holder.start()
    time.sleep(0.05)
    try:
        outcome, _ = g.fetch(["q-other"], lambda argv: Outcome("ok"))
    finally:
        release.set()
        holder.join()
    assert outcome.exit_code == 503
    assert outcome.error is not None and outcome.error.startswith("busy:")
    assert g.launched == 1
    assert len(g._starts) == 1  # the refused call did not spend rate budget
