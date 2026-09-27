"""Command line entry point — also the way to exercise the server without MCP."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .execute import Limits, configure, slurm_available
from .guard import ALLOWED
from .server import call_tool, describe, footprint, tool_definitions
from .topics import TOPICS, topic_names

#: The distribution name. `slurm-mcp` on PyPI is an unrelated project, so the
#: distribution is named this, while the command stays `slurm-mcp`. It is not
#: on PyPI yet (https://pypi.org/pypi/slurm-readonly-mcp/json returned 404 on
#: 2026-09-26), so nothing here may tell a user to `pip install` it bare: an
#: unregistered name is one anybody could register.
DIST = "slurm-readonly-mcp"
SOURCE = "https://github.com/Zhanyl-tech/slurm-mcp"
#: The install commands `serve` prints when the [server] extra is missing. The
#: README shows the same git command; a test keeps the two in step.
SERVER_INSTALL = (
    f'pip install "{DIST}[server] @ git+{SOURCE}"',
    "pip install -e '.[server]'   (from a checkout; or: make serve)",
)


def _non_negative_float(text: str) -> float:
    value = float(text)
    if not value >= 0:  # also refuses nan
        raise argparse.ArgumentTypeError(f"must be >= 0, got {text}")
    return value


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {text}")
    return value


def _row_cap(text: str) -> int:
    # 0 is refused rather than read as "no cap": it would keep no rows, and a
    # queue that is not empty would come back as "(no rows)".
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be >= 1 (there is no 'no cap' value; pass a larger number), got {text}"
        )
    return value


def _print_footprint() -> None:
    f = footprint()
    resident = f["resident_total"]
    rows = [
        ("resident: three tool descriptors", f["resident_tool_list"]),
        ("resident: server instructions", f["resident_instructions"]),
        ("resident total", resident),
        ("detail, fetched on request", f["on_request_detail"]),
        (f"flat, {f['flat_binaries']} schemas only", f["flat_schemas_only"]),
        ("flat, schemas + inlined detail", f["flat_with_inlined_detail"]),
    ]
    for label, chars in rows:
        ratio = f"  ({chars / resident:.2f}x resident)" if label.startswith("flat") else ""
        print(f"  {label:<34}{chars:>6} chars{ratio}")
    print(
        "\n  The second flat line assumes a flat server would carry guidance comparable"
        "\n  to slurm_describe in its tool descriptions. Without that assumption, compare"
        "\n  the schemas-only line."
    )


def _print_surface() -> None:
    print(f"slurm on PATH: {slurm_available()}\n")
    print("permitted binaries, and the exact options each may carry:")
    options = 0
    for name, spec in sorted(ALLOWED.items()):
        if spec.show_entities:
            shape = f"show <{'|'.join(sorted(spec.show_entities))}> [name]"
        else:
            shape = " ".join(sorted(spec.options)) or "(no options)"
        options += len(spec.options)
        print(f"  {name:<9} {shape}")
    print(
        f"\n{len(ALLOWED)} binaries, {options} options. Anything else — another binary, "
        f"an abbreviation, an option before a subcommand, a bare scontrol — is refused."
    )
    print(f"topics ({len(TOPICS)}): {', '.join(topic_names())}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slurm-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="serve the hand-written illustrative fixtures instead of reading a cluster. "
        "Every response is labelled [fixture] so it cannot be mistaken for a live read.",
    )
    d = Limits()
    parser.add_argument(
        "--cache-ttl",
        type=_non_negative_float,
        default=d.cache_ttl,
        metavar="SECONDS",
        help=f"reuse a live answer for the identical command this long; 0 disables "
        f"(default {d.cache_ttl:g})",
    )
    parser.add_argument(
        "--max-concurrent",
        type=_non_negative_int,
        default=d.max_concurrent,
        metavar="N",
        help=f"Slurm commands allowed to run at once (default {d.max_concurrent})",
    )
    parser.add_argument(
        "--max-calls-per-minute",
        type=_non_negative_int,
        default=d.max_calls_per_minute,
        metavar="N",
        help=f"Slurm commands allowed to start per 60s; 0 disables (default "
        f"{d.max_calls_per_minute})",
    )
    parser.add_argument(
        "--max-rows",
        type=_row_cap,
        default=d.max_rows,
        metavar="N",
        help=f"rows returned for tabular topics before truncating; at least 1, with no "
        f"'unlimited' value (default {d.max_rows})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server over stdio (needs the [server] extra)")
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
    fx: bool = args.fixtures
    configure(
        Limits(
            cache_ttl=args.cache_ttl,
            max_concurrent=max(1, args.max_concurrent),
            max_calls_per_minute=args.max_calls_per_minute,
            max_rows=args.max_rows,
        )
    )

    if args.cmd == "serve":
        try:
            import mcp  # noqa: F401
        except ImportError:
            print(
                "slurm-mcp serve needs the MCP SDK, which is the optional [server] extra:\n"
                + "\n".join(f"  {cmd}" for cmd in SERVER_INSTALL),
                file=sys.stderr,
            )
            return 2
        import asyncio

        from .server import serve

        asyncio.run(serve(use_fixtures=fx))
        return 0

    if args.cmd == "tools":
        print(json.dumps(tool_definitions(), indent=2))
        return 0

    if args.cmd == "surface":
        _print_surface()
        return 0

    if args.cmd == "footprint":
        _print_footprint()
        return 0

    if args.cmd == "describe":
        print(describe(args.topic))
        return 0

    if args.cmd == "overview":
        reply = call_tool("slurm_overview", {}, use_fixtures=fx)
    else:  # query
        filters = {}
        for item in args.filter:
            if "=" not in item:
                parser.error(f"--filter expects KEY=VALUE, got {item!r}")
            k, v = item.split("=", 1)
            filters[k] = v
        reply = call_tool("slurm_query", {"topic": args.topic, "filters": filters}, use_fixtures=fx)

    # Non-zero when any read failed, so scripts and CI can tell "the read
    # failed" from "the read returned nothing".
    print(reply.text)
    return 1 if reply.is_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
