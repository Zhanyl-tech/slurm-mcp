"""The Makefile's venv bootstrap, run for real with stub Pythons.

Only the pip path (no uv) is exercised: it is the one with a multi-command
MAKE_VENV, and so the one where shell precedence can go wrong. The stubs log
how they were called, so the test sees exactly which commands the recipe ran.
Skipped where ``make`` is not installed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _project(tmp_path: Path, *, version_ok: bool) -> tuple[Path, Path]:
    """A scratch copy of the Makefile, and a stub `$(PY)` that logs its calls.

    The stub answers the >=3.11 check with ``version_ok`` and, for ``-m venv
    DIR``, installs ``DIR/bin/python`` as a second logging stub (``VENVPY``).
    """
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    log = tmp_path / "calls.log"
    log.touch()
    _executable(tmp_path / "venv-python-template", f'echo "VENVPY $*" >> {log}')
    _executable(
        tmp_path / "stub-python",
        f'echo "PY $*" >> {log}\n'
        'case "$1" in\n'
        f"  -c) exit {0 if version_ok else 1} ;;\n"
        f'  -m) mkdir -p "$3/bin" && cp {tmp_path / "venv-python-template"} "$3/bin/python" ;;\n'
        "esac",
    )
    return tmp_path / "stub-python", log


def _make_install(tmp_path: Path, py: Path) -> subprocess.CompletedProcess[str]:
    # A parent `make test` must not leak its flags into this one.
    env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL")}
    return subprocess.run(
        ["make", "install", "UV=", f"PY={py}"],  # UV= forces the pip path
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_an_existing_venv_is_not_recreated(tmp_path: Path) -> None:
    """Rebuilding after a pyproject.toml change must reuse .venv, not rerun venv over it.

    Before the fix, `test -x python || check && venv && pip` ran as
    `(test -x python || check) && venv && pip`: the version check was skipped
    and `$(PY) -m venv .venv` rewrote pyvenv.cfg for a different Python.
    """
    py, log = _project(tmp_path, version_ok=True)
    _executable(tmp_path / ".venv" / "bin" / "slurm-mcp", "exit 0")
    shutil.copy(tmp_path / "venv-python-template", tmp_path / ".venv" / "bin" / "python")
    old = (tmp_path / "pyproject.toml").stat().st_mtime - 60
    os.utime(tmp_path / ".venv" / "bin" / "slurm-mcp", (old, old))

    proc = _make_install(tmp_path, py)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines()
    assert not [c for c in calls if c.startswith("PY ")], calls
    assert any("-m pip install -q -e .[dev]" in c for c in calls), calls


def test_a_too_old_python_is_refused_before_any_venv_exists(tmp_path: Path) -> None:
    py, log = _project(tmp_path, version_ok=False)
    proc = _make_install(tmp_path, py)
    assert proc.returncode != 0
    calls = log.read_text().splitlines()
    assert [c for c in calls if c.startswith("PY ")] == [calls[0]]
    assert calls[0].startswith("PY -c ")
    assert not (tmp_path / ".venv").exists()


def test_a_missing_venv_is_created_after_the_version_check(tmp_path: Path) -> None:
    py, log = _project(tmp_path, version_ok=True)
    proc = _make_install(tmp_path, py)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("PY -c ")
    assert calls[1] == "PY -m venv .venv"
    assert calls[2] == "VENVPY -m pip install -q --upgrade pip"
    assert (tmp_path / ".venv" / "bin" / "slurm-mcp").exists()
