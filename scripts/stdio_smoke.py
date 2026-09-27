"""Drive slurm-mcp over real stdio with the MCP SDK's own client, against fixtures.

    python scripts/stdio_smoke.py                     # python -m slurm_mcp.cli --fixtures serve
    python scripts/stdio_smoke.py slurm-mcp --fixtures serve   # an installed command

This is the check that the transport the repo is named after actually works:
a subprocess, a real stdio pipe, the SDK's handshake, and the answers an agent
would see. It needs the ``server`` extra and no Slurm. Exit status is 0 only if
every check passes. CI runs it against a freshly built wheel.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

EXPECTED_TOOLS = ["slurm_overview", "slurm_query", "slurm_describe"]


def _text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def run(command: list[str], env: dict[str, str] | None = None) -> list[tuple[str, bool, str]]:
    """Run every check against ``command``; ``env`` adds variables for the child.

    The SDK starts the child with a short default environment (on POSIX: HOME,
    LOGNAME, PATH, SHELL, TERM, USER; mcp 2.0.0 and 2.2.0 alike) and merges
    ``env`` over it. So PYTHONPATH, and pytest's ``pythonpath`` setting, reach
    the child only when passed here.
    """
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.shared.exceptions import MCPError

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: object, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        info = init.server_info
        check("serverInfo.name is slurm-mcp", info.name == "slurm-mcp", info.name)
        check("serverInfo.version is set", bool(info.version), repr(info.version))
        check("server instructions are sent", "Read-only" in (init.instructions or ""))

        tools = (await session.list_tools()).tools
        names = [t.name for t in tools]
        check("exactly three tools", names == EXPECTED_TOOLS, repr(names))
        check(
            "every tool is annotated readOnlyHint=true",
            all(t.annotations is not None and t.annotations.read_only_hint for t in tools),
        )

        r = await session.call_tool("slurm_overview", {})
        body = _text(r)
        check(
            "slurm_overview answers from fixtures",
            not r.is_error and body.count("[fixture]") == 3,
            body[:120],
        )

        r = await session.call_tool("slurm_query", {"topic": "queue", "filters": {"user": "bob"}})
        # Data rows only: the `[fixture] $ squeue ...` header also contains pipes.
        rows = [
            line
            for line in _text(r).splitlines()
            if line.count("|") == 6 and not line.startswith("[")
        ]
        check(
            "a user filter returns only that user's rows",
            not r.is_error and rows and all(line.split("|")[2] == "bob" for line in rows),
            repr(rows),
        )

        r = await session.call_tool(
            "slurm_query", {"topic": "queue", "filters": {"user": "--format=$(id)"}}
        )
        check("an injection attempt is a tool error (isError)", r.is_error, _text(r)[:120])

        r = await session.call_tool("slurm_query", {"topic": "queue", "filters": "notadict"})
        check("malformed filters are a tool error, not a crash", r.is_error, _text(r)[:120])

        r = await session.call_tool("slurm_describe", {"topic": "config"})
        check("slurm_describe returns detail", not r.is_error and "# config" in _text(r))

        try:
            await session.call_tool("slurm_delete_everything", {})
            check("an unknown tool is a protocol error", False, "no error raised")
        except MCPError as exc:
            check("an unknown tool is a protocol error", exc.code == -32602, str(exc))

    return checks


def main(argv: list[str]) -> int:
    command = argv or [sys.executable, "-m", "slurm_mcp.cli", "--fixtures", "serve"]
    checks = asyncio.run(run(command))
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -- {detail}"))
    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} stdio checks passed against: {command}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
