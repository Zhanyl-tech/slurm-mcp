"""The MCP transport, driven by the SDK's own client.

Skipped when the ``server`` extra is not installed, so the guard can still be
proven on a machine with no MCP at all. CI's server job sets
SLURM_MCP_REQUIRE_TRANSPORT=1, which turns that skip into a failure, so these
tests cannot silently stop running there.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

try:
    import mcp  # noqa: F401
except ImportError:
    if os.environ.get("SLURM_MCP_REQUIRE_TRANSPORT"):
        raise
    pytest.skip("the [server] extra (mcp) is not installed", allow_module_level=True)

from mcp.client import Client
from mcp.shared.exceptions import MCPError

from slurm_mcp import __version__
from slurm_mcp.server import build_server

FakeBin = Callable[[str, str], Path]
ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "scripts" / "stdio_smoke.py"
SERVE_FROM_SOURCE = [sys.executable, "-m", "slurm_mcp.cli"]


def _text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def _source_tree_env(**extra: str) -> dict[str, str]:
    """Environment for a child server run from this checkout.

    pytest's ``pythonpath = ["src"]`` only changes this process's sys.path, and
    the SDK passes a child just a short default environment. Without this, the
    stdio tests passed only where slurm_mcp happened to be pip-installed.
    """
    src = str(ROOT / "src")
    inherited = os.environ.get("PYTHONPATH")
    return {"PYTHONPATH": os.pathsep.join([src, inherited]) if inherited else src, **extra}


def test_in_process_handshake_lists_three_read_only_tools() -> None:
    async def go() -> tuple[Any, Any]:
        async with Client(build_server(use_fixtures=True)) as client:
            return client.server_info, (await client.list_tools()).tools

    info, tools = asyncio.run(go())
    assert info is not None
    assert info.name == "slurm-mcp"
    assert info.version == __version__
    assert [t.name for t in tools] == ["slurm_overview", "slurm_query", "slurm_describe"]
    for t in tools:
        assert t.annotations is not None
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


def test_in_process_query_round_trip_and_is_error() -> None:
    async def go() -> list[Any]:
        async with Client(build_server(use_fixtures=True)) as client:
            return [
                await client.call_tool(
                    "slurm_query", {"topic": "queue", "filters": {"user": "bob"}}
                ),
                await client.call_tool(
                    "slurm_query", {"topic": "queue", "filters": {"user": "; rm -rf /"}}
                ),
                await client.call_tool("slurm_query", {"topic": "queue", "filters": "notadict"}),
            ]

    ok, denied, malformed = asyncio.run(go())
    assert not ok.is_error
    assert _text(ok).startswith("[fixture]") and "alice" not in _text(ok)
    assert denied.is_error and "refused" in _text(denied)
    assert malformed.is_error


def test_unknown_tool_is_a_protocol_error() -> None:
    """The spec lists an unknown tool under protocol errors (JSON-RPC -32602)."""

    async def go() -> int:
        async with Client(build_server(use_fixtures=True)) as client:
            try:
                await client.call_tool("slurm_delete_everything", {})
            except MCPError as exc:
                return int(exc.code)
        raise AssertionError("no protocol error for an unknown tool")

    assert asyncio.run(go()) == -32602


def test_a_slow_slurm_call_does_not_block_the_event_loop(fakebin: FakeBin) -> None:
    """v0.1.0 ran subprocess.run inside the async handler, freezing the whole
    server for as long as Slurm took to answer. Now a second request (here a
    slurm_describe, which touches no Slurm) is served while squeue is running.

    (`ping` would be the obvious probe, but the 2026-07-28 protocol removed it.)
    """
    fakebin("squeue", "sleep 1; echo '1|gpu|alice|RUNNING|0:01|1|n1'")

    async def go() -> tuple[float, bool, Any, Any]:
        async with Client(build_server()) as client:
            started = time.monotonic()
            slow = asyncio.create_task(client.call_tool("slurm_query", {"topic": "queue"}))
            # With a blocking handler the loop freezes *here*, inside the sleep,
            # until squeue returns; so time is measured from the slow call's start.
            await asyncio.sleep(0.2)
            quick = await client.call_tool("slurm_describe", {"topic": "config"})
            elapsed = time.monotonic() - started
            still_running = not slow.done()
            return elapsed, still_running, quick, await slow

    elapsed, still_running, quick, result = asyncio.run(go())
    assert not result.is_error, _text(result)
    assert not quick.is_error
    # Measured in this session: ~0.21s threaded; ~1.02s when the handler ran inline.
    assert still_running, "the describe call waited for squeue to finish"
    assert elapsed < 0.7, f"describe answered {elapsed:.2f}s after the slow call started"


def test_real_stdio_subprocess_passes_the_smoke_checks() -> None:
    """The same checks CI runs against the built wheel, here from the source tree."""
    spec = importlib.util.spec_from_file_location("stdio_smoke", SMOKE)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    checks = asyncio.run(smoke.run([*SERVE_FROM_SOURCE, "--fixtures", "serve"], _source_tree_env()))
    failed = [(name, detail) for name, ok, detail in checks if not ok]
    assert not failed
    assert len(checks) >= 10


def test_the_stdio_audit_log_is_one_json_object_per_line(fakebin: FakeBin, tmp_path: Path) -> None:
    """What `serve` really writes to stderr, parsed the way a log shipper would.

    An earlier draft of this branch prefixed every line with `slurm-mcp `, so
    json.loads (or jq) failed on each one although the README said "one JSON
    line". Cache hits are logged too, with "cached": true.
    """
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    squeue = fakebin("squeue", "echo '1|gpu|alice|RUNNING|0:01|1|n1'")
    path = os.pathsep.join([str(squeue.parent), os.environ.get("PATH", "")])
    params = StdioServerParameters(
        command=SERVE_FROM_SOURCE[0],
        args=[*SERVE_FROM_SOURCE[1:], "serve"],  # live mode: the fake squeue answers
        env=_source_tree_env(PATH=path),
    )
    stderr_path = tmp_path / "server.stderr"

    async def go() -> None:
        with stderr_path.open("w") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                for _ in range(2):  # the second is answered by the cache
                    r = await session.call_tool("slurm_query", {"topic": "queue"})
                    assert not r.is_error, _text(r)

    asyncio.run(go())
    lines = [line for line in stderr_path.read_text().splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    assert [r["cached"] for r in records] == [False, True], lines
    assert records[0]["argv"][0] == "squeue" and records[0]["exit_code"] == 0
