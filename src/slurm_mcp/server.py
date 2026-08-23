"""The MCP server: three tools, and the allowlist underneath all of them.

Kept deliberately thin. Everything that decides what is permitted lives in
:mod:`slurm_mcp.guard`, and everything that decides what is worth reading lives
in :mod:`slurm_mcp.topics`. This module only adapts them to MCP, so the
security properties can be tested without an MCP client in the loop — which is
what the test suite does.
"""

from __future__ import annotations

from typing import Any

from .execute import overview, run_topic
from .topics import TOPICS, topic_names

SERVER_NAME = "slurm-mcp"
INSTRUCTIONS = (
    "Read-only access to a Slurm cluster. Start with slurm_overview for a snapshot, "
    "then slurm_query for a specific topic. Call slurm_describe only when you need a "
    "topic's column meanings or filters — it exists so that detail is not in your "
    "context by default. Nothing here can change cluster state; mutating commands are "
    "refused by the server, not discouraged by this text."
)


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
            "inputSchema": {"type": "object", "properties": {}, "required": []},
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
            },
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
            },
        },
    ]


def describe(topic_name: str) -> str:
    topic = TOPICS.get(topic_name)
    if topic is None:
        return f"unknown topic {topic_name!r}; have {', '.join(topic_names())}"
    filters = ", ".join(sorted(topic.filters)) or "none"
    return (
        f"# {topic.name}\n\n{topic.summary}\n\n"
        f"command: {' '.join(topic.argv)}\n"
        f"filters: {filters}\n\n{topic.detail}"
    )


def call_tool(name: str, arguments: dict[str, Any], *, use_fixtures: bool = False) -> str:
    """Dispatch one tool call. Pure enough to test without an MCP transport."""
    if name == "slurm_overview":
        return overview(use_fixtures=use_fixtures)
    if name == "slurm_describe":
        return describe(str(arguments.get("topic", "")))
    if name == "slurm_query":
        topic = str(arguments.get("topic", ""))
        raw = arguments.get("filters") or {}
        filters = {str(k): str(v) for k, v in dict(raw).items()}
        try:
            return run_topic(topic, filters, use_fixtures=use_fixtures).render()
        except KeyError as exc:
            return f"error: {exc}"
    return f"unknown tool {name!r}"


async def serve(*, use_fixtures: bool = False) -> None:  # pragma: no cover - transport
    """Run over stdio. Requires the optional `server` extra.

    Written against the constructor-callback API (`on_list_tools`/`on_call_tool`)
    rather than the older `@server.list_tools()` decorators, which do not exist
    on the installed mcp and would fail at import time.
    """
    import mcp.types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    async def on_list_tools(ctx: object, params: object) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=d["name"],
                    description=d["description"],
                    input_schema=d["inputSchema"],
                )
                for d in tool_definitions()
            ]
        )

    async def on_call_tool(
        ctx: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        text = call_tool(params.name, dict(params.arguments or {}), use_fixtures=use_fixtures)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server: Server[None] = Server(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def footprint() -> dict[str, int]:
    """Measure what progressive disclosure actually buys, in characters.

    The README claims three tools cost less resident context than one tool per
    binary with its detail attached. This is how that number is produced, so a
    reader can rerun it rather than believe it.
    """
    import json

    from .guard import ALLOWED

    resident = len(json.dumps(tool_definitions()))
    on_request = sum(len(describe(t)) for t in topic_names())
    flat_tools = json.dumps(
        [
            {
                "name": name,
                "description": tool.description,
                "inputSchema": {
                    "type": "object",
                    "properties": {"args": {"type": "array", "items": {"type": "string"}}},
                },
            }
            for name, tool in ALLOWED.items()
        ]
    )
    return {
        "resident_three_tools": resident,
        "on_request_detail": on_request,
        "flat_surface_always_resident": len(flat_tools) + on_request,
    }
