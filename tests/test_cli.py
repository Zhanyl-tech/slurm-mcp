"""The command line, driven through main() — exit codes are part of the contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from slurm_mcp import __version__, execute
from slurm_mcp.cli import main

README = Path(__file__).resolve().parent.parent / "README.md"


def test_fixture_overview_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fixtures", "overview"]) == 0
    out = capsys.readouterr().out
    assert out.count("[fixture]") == 3
    assert "no fixture" not in out


def test_a_failed_live_read_exits_non_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.1.0 exited 0 while printing 'sinfo not found' three times."""
    monkeypatch.setenv("PATH", "/nonexistent")
    assert main(["overview"]) == 1
    assert "sinfo not found" in capsys.readouterr().out


def test_query_with_a_filter(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fixtures", "query", "queue", "--filter", "user=mchen"]) == 0
    out = capsys.readouterr().out
    assert "-u mchen" in out and "arun" not in out


def test_query_with_an_unsafe_filter_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fixtures", "query", "queue", "--filter", "user=$(id)"]) == 1
    assert "refused" in capsys.readouterr().out


def test_query_with_an_unknown_filter_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fixtures", "query", "config", "--filter", "user=alice"]) == 1
    assert "has no filter 'user'" in capsys.readouterr().out


def test_malformed_filter_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--fixtures", "query", "queue", "--filter", "user"])
    assert exc.value.code == 2


def test_describe_tools_surface_and_footprint(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["describe", "queue"]) == 0
    assert main(["tools"]) == 0
    assert main(["surface"]) == 0
    assert main(["footprint"]) == 0
    out = capsys.readouterr().out
    assert "# queue" in out
    assert '"readOnlyHint": true' in out
    assert "7 binaries" in out and "bare scontrol" in out
    assert "x resident" in out


def test_limits_flags_configure_the_gate() -> None:
    main(
        [
            "--cache-ttl",
            "0",
            "--max-concurrent",
            "1",
            "--max-calls-per-minute",
            "5",
            "--max-rows",
            "3",
            "--fixtures",
            "describe",
            "queue",
        ]
    )
    limits = execute.gate().limits
    assert (limits.cache_ttl, limits.max_concurrent, limits.max_calls_per_minute) == (0, 1, 5)
    assert limits.max_rows == 3


def test_negative_limits_are_refused() -> None:
    with pytest.raises(SystemExit):
        main(["--cache-ttl", "-1", "surface"])


def test_a_row_cap_of_zero_is_refused_not_rendered_as_no_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--cache-ttl 0` and `--max-calls-per-minute 0` disable those limits, so 0
    is a natural thing to try here. It used to keep zero rows and print
    "(no rows)" for a queue with six jobs in it, and exit 0."""
    with pytest.raises(SystemExit) as exc:
        main(["--max-rows", "0", "--fixtures", "query", "queue"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "must be >= 1" in captured.err
    assert "(no rows)" not in captured.out


def test_a_row_cap_of_one_shows_a_row_and_says_it_truncated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--max-rows", "1", "--fixtures", "query", "queue"]) == 0
    out = capsys.readouterr().out
    assert "(no rows)" not in out
    assert "[truncated: 1 of 6 rows shown" in out


def test_truncation_keeps_the_fixture_filter_note(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--max-rows", "1", "--fixtures", "query", "queue", "--filter", "user=mchen"]) == 0
    out = capsys.readouterr().out
    assert "[fixture] user=mchen was applied by this server" in out
    assert "[truncated: 1 of 2 rows shown; narrow with filters: partition, user]" in out


def test_serve_without_the_server_extra_says_how_to_install_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instead of a raw ModuleNotFoundError traceback, commands that work today.

    The distribution is not on PyPI, so a bare `pip install 'slurm-readonly-mcp[server]'`
    fails, and would install whoever registered the name first. The git form
    printed here is the one the README documents.
    """
    monkeypatch.setitem(sys.modules, "mcp", None)  # makes `import mcp` raise ImportError
    assert main(["serve"]) == 2
    err = capsys.readouterr().err
    git_form = (
        'pip install "slurm-readonly-mcp[server] @ git+https://github.com/Zhanyl-tech/slurm-mcp"'
    )
    assert git_form in err
    assert git_form in README.read_text(encoding="utf-8")
    assert "pip install -e '.[server]'" in err
    assert "install 'slurm-readonly-mcp[server]'" not in err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
