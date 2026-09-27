"""The MCP server: three tools, and the allowlist underneath all of them.

Kept deliberately thin. Everything that decides what is permitted lives in
:mod:`slurm_mcp.guard`, and everything that decides what is worth reading lives
in :mod:`slurm_mcp.topics`. This module only adapts them to MCP, so the
security properties can be tested without an MCP client in the loop — which is
what most of the test suite does. ``tests/test_transport.py`` then drives the
real SDK, in-process and over stdio, when the ``server`` extra is installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import __version__
from .execute import audit_handler, overview, render_overview, run_topic
from .guard import ALLOWED
from .topics import TOPICS, topic_names

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

SERVER_NAME = "slurm-mcp"
INSTRUCTIONS = (
    "Read-only access to a Slurm cluster. Start with slurm_overview for a snapshot, "
    "then slurm_query for a specific topic. Call slurm_describe only when you need a "
    "topic's column meanings or filters — it exists so that detail is not in your "
    "context by default. Nothing here can change cluster state; mutating commands are "
    "refused by the server, not discouraged by this text."
)

#: MCP tool annotations. The spec says clients MUST treat annotations as
#: untrusted unless the server is trusted, so these are a courtesy to clients
#: that gate approval on them (readOnlyHint defaults to false, i.e. "may
#: write"), never the control. The guard is the control.
READ_ONLY_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@dataclass(frozen=True)
class Reply:
    """One tool result: the text, and whether it reports a failure.

    ``is_error`` becomes MCP's ``isError`` so a client can tell "the read failed"
    from "the read returned nothing" without parsing prose.
    """

    text: str
    is_error: bool = False


def tool_definitions() -> list[dict[str, Any]]:
    """The three tools, as MCP tool descriptors."""
    names = topic_names()
    return [
        {
            "name": "slurm_overview",
            "description": (
                "Cluster snapshot: node states, the queue, and scheduler diagnostics "
                "in one call. Start here."
            ),
            # The spec's recommended schema for a tool with no parameters.
            "inputSchema": {"type": "object", "additionalProperties": False},
            "annotations": READ_ONLY_ANNOTATIONS,
        },
        {
            "name": "slurm_query",
            "description": (
                "Read one topic. Topics: " + ", ".join(names) + ". "
                "Filters are optional and topic-specific; slurm_describe lists them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "enum": names},
                    "filters": {
                        "type": "object",
                        "description": 'Topic-specific filters, e.g. {"user": "alice"}.',
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
            "annotations": READ_ONLY_ANNOTATIONS,
        },
        {
            "name": "slurm_describe",
            "description": (
                "Column meanings, filters and interpretation notes for one topic. "
                "Fetch only when you need them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"topic": {"type": "string", "enum": names}},
                "required": ["topic"],
                "additionalProperties": False,
            },
            "annotations": READ_ONLY_ANNOTATIONS,
        },
    ]


TOOL_NAMES = frozenset(d["name"] for d in tool_definitions())


def describe(topic_name: str) -> str:
    topic = TOPICS.get(topic_name)
    if topic is None:
        return f"unknown topic {topic_name!r}; have {', '.join(topic_names())}"
    filters = ", ".join(sorted(topic.filters)) or "none"
    columns = f"columns: {'|'.join(topic.columns)}\n" if topic.columns else ""
    return (
        f"# {topic.name}\n\n{topic.summary}\n\n"
        f"command: {' '.join(topic.argv)}\n"
        f"{columns}"
        f"filters: {filters}\n\n{topic.detail}"
    )


def _unexpected(arguments: dict[str, Any], allowed: set[str]) -> Reply | None:
    extra = sorted(set(arguments) - allowed)
    if extra:
        return Reply(
            f"error: unexpected argument(s) {', '.join(extra)}; "
            f"accepts {', '.join(sorted(allowed)) or 'none'}",
            True,
        )
    return None


def _topic_arg(arguments: dict[str, Any]) -> str | Reply:
    topic = arguments.get("topic")
    if not isinstance(topic, str) or topic not in TOPICS:
        return Reply(f"error: topic must be one of {', '.join(topic_names())}; got {topic!r}", True)
    return topic


def call_tool(name: str, arguments: Any, *, use_fixtures: bool = False) -> Reply:
    """Dispatch one tool call. Pure enough to test without an MCP transport.

    Validates every input itself (the MCP spec says servers MUST), because the
    input schema is advice to the client, not something the SDK enforces here.
    """
    if not isinstance(arguments, dict):
        return Reply("error: arguments must be a JSON object", True)

    if name == "slurm_overview":
        if bad := _unexpected(arguments, set()):
            return bad
        results = overview(use_fixtures=use_fixtures)
        return Reply(render_overview(results), any(r.failed for r in results))

    if name == "slurm_describe":
        if bad := _unexpected(arguments, {"topic"}):
            return bad
        topic = _topic_arg(arguments)
        if isinstance(topic, Reply):
            return topic
        return Reply(describe(topic))

    if name == "slurm_query":
        if bad := _unexpected(arguments, {"topic", "filters"}):
            return bad
        topic = _topic_arg(arguments)
        if isinstance(topic, Reply):
            return topic
        raw = arguments.get("filters")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            return Reply(
                'error: filters must be an object of string to string, e.g. {"user": "alice"}',
                True,
            )
        try:
            result = run_topic(topic, raw, use_fixtures=use_fixtures)
        except KeyError as exc:
            return Reply(f"error: {exc.args[0]}", True)
        except ValueError as exc:
            return Reply(f"error: {exc}", True)
        return Reply(result.render(), result.failed)

    return Reply(f"unknown tool {name!r}; have {', '.join(sorted(TOOL_NAMES))}", True)


def build_server(*, use_fixtures: bool = False) -> Server[Any]:
    """The MCP server object, without a transport. Requires the ``server`` extra.

    Written against the mcp 2.x low-level API: constructor callbacks
    (``on_list_tools``/``on_call_tool``) and snake_case model fields. mcp 1.x
    has neither and fails at construction, which is why pyproject pins
    ``mcp>=2.0,<3``. Tests connect to this in-process; :func:`serve` adds stdio.
    """
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.shared.exceptions import MCPError

    tools = [
        types.Tool(
            name=d["name"],
            description=d["description"],
            input_schema=d["inputSchema"],
            annotations=types.ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        for d in tool_definitions()
    ]

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in TOOL_NAMES:
            # The spec lists an unknown tool as a protocol error, not a tool error.
            raise MCPError(types.INVALID_PARAMS, f"unknown tool {params.name!r}")
        # Slurm calls block for up to TIMEOUT_SECONDS. Running them in a worker
        # thread keeps the event loop free, so pings, other requests and
        # cancellation are still served while slurmctld is slow.
        reply = await asyncio.to_thread(
            call_tool, params.name, params.arguments or {}, use_fixtures=use_fixtures
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=reply.text)],
            is_error=reply.is_error,
        )

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve(*, use_fixtures: bool = False) -> None:  # pragma: no cover - needs a stdio pipe
    """Run over stdio. Exercised by ``scripts/stdio_smoke.py`` and the transport tests."""
    from mcp.server.stdio import stdio_server

    logger = logging.getLogger("slurm_mcp")
    logger.addHandler(audit_handler(sys.stderr))
    logger.setLevel(logging.INFO)

    server = build_server(use_fixtures=use_fixtures)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def footprint() -> dict[str, int]:
    """Measure what progressive disclosure actually buys, in characters.

    The README compares the resident cost of this server's three tools with a
    hypothetical flat server exposing one tool per binary. This is how those
    numbers are produced, so a reader can rerun them rather than believe them.

    Two flat baselines, because the comparison turns on an assumption:

    * ``flat_schemas_only`` — one generic ``args`` tool per binary the topics
      use, with the same annotations. No guidance at all.
    * ``flat_with_inlined_detail`` — the same, plus this server's own
      ``slurm_describe`` text, *assuming* a flat server would carry comparable
      per-binary guidance in its descriptions. That assumption is not measured
      against any real server.

    The resident side counts the server instructions too, since they are sent
    at initialize; the flat baselines are given no instructions, which favours
    the flat design.
    """
    tool_list = len(json.dumps(tool_definitions()))
    on_request = sum(len(describe(t)) for t in topic_names())
    binaries = sorted({t.argv[0] for t in TOPICS.values()})
    flat_tools = json.dumps(
        [
            {
                "name": name,
                "description": ALLOWED[name].description,
                "inputSchema": {
                    "type": "object",
                    "properties": {"args": {"type": "array", "items": {"type": "string"}}},
                },
                "annotations": READ_ONLY_ANNOTATIONS,
            }
            for name in binaries
        ]
    )
    return {
        "resident_tool_list": tool_list,
        "resident_instructions": len(INSTRUCTIONS),
        "resident_total": tool_list + len(INSTRUCTIONS),
        "on_request_detail": on_request,
        "flat_binaries": len(binaries),
        "flat_schemas_only": len(flat_tools),
        "flat_with_inlined_detail": len(flat_tools) + on_request,
    }
