from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from slurm_mcp import execute
from slurm_mcp.execute import Limits

#: A callable that writes an executable shell script named after a Slurm binary
#: into a directory that is first on PATH, and returns its path.
FakeBin = Callable[[str, str], Path]


@pytest.fixture(autouse=True)
def fresh_gate() -> Iterator[None]:
    """Every test starts with an empty cache and an unused rate budget."""
    execute.configure(Limits())
    yield
    execute.configure(Limits())


@pytest.fixture
def fakebin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBin:
    """Fake Slurm binaries: real subprocesses, real PATH lookup, no cluster.

    The live path is exercised exactly as it runs in production, with
    subprocess.run finding the binary on PATH; only the binary is fake.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    def make(name: str, body: str) -> Path:
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    return make
