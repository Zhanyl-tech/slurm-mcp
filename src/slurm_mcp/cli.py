"""Command line entry point — also the way to exercise the server without MCP."""

from __future__ import annotations

import argparse
import json

from .execute import slurm_available
from .guard import ALLOWED
from .server import call_tool, describe, footprint, tool_definitions
from .topics import TOPICS, topic_names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slurm-mcp", description=__doc__)
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="serve recorded fixtures instead of reading a cluster. Every response "
        "is labelled [fixture] so it cannot be mistaken for a live read.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server over stdio")
    sub.add_parser("tools", help="print the three tool descriptors as MCP sees them")
    sub.add_parser("surface", help="print the read-only allowlist")
    sub.add_parser("footprint", help="measure the context cost of the tool surface")
    sub.add_parser("overview", help="cluster snapshot")
    p_q = sub.add_parser("query", help="read one topic")
    p_q.add_argument("topic", choices=topic_names())
    p_q.add_argument("--filter", action="append", default=[], metavar="KEY=VALUE")
    p_d = sub.add_parser("describe", help="detail for one topic")
    p_d.add_argument("topic", choices=topic_names())

    args = parser.parse_args(argv)
    fx = args.fixtures

    if args.cmd == "serve":
        import asyncio

        from .server import serve

        asyncio.run(serve(use_fixtures=fx))
        return 0

    if args.cmd == "tools":
        print(json.dumps(tool_definitions(), indent=2))
        return 0

    if args.cmd == "surface":
        print(f"slurm on PATH: {slurm_available()}\n")
        print("permitted, read paths only:")
        for name, tool in sorted(ALLOWED.items()):
            note = ""
            if tool.forbidden_subcommands:
                note = f"   denied: {', '.join(sorted(tool.forbidden_subcommands))}"
            print(f"  {name:<10} {tool.description}{note}")
        print(f"\ntopics: {', '.join(topic_names())}")
        return 0

    if args.cmd == "footprint":
        f = footprint()
        print(f"  resident, three tools          {f['resident_three_tools']:>6} chars")
        print(f"  detail, fetched on request     {f['on_request_detail']:>6} chars")
        print(
            f"  flat one-tool-per-binary       {f['flat_surface_always_resident']:>6} chars"
            f"  ({f['flat_surface_always_resident'] / f['resident_three_tools']:.1f}x resident)"
        )
        return 0

    if args.cmd == "overview":
        print(call_tool("slurm_overview", {}, use_fixtures=fx))
        return 0

    if args.cmd == "describe":
        print(describe(args.topic))
        return 0

    if args.cmd == "query":
        filters = {}
        for item in args.filter:
            if "=" not in item:
                parser.error(f"--filter expects KEY=VALUE, got {item!r}")
            k, v = item.split("=", 1)
            filters[k] = v
        allowed = TOPICS[args.topic].filters
        for k in filters:
            if k not in allowed:
                parser.error(
                    f"topic {args.topic!r} has no filter {k!r}; "
                    f"accepts {', '.join(sorted(allowed)) or 'none'}"
                )
        print(call_tool("slurm_query", {"topic": args.topic, "filters": filters}, use_fixtures=fx))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
