"""Tool dispatch and descriptors, without an MCP transport."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from slurm_mcp.server import (
    INSTRUCTIONS,
    call_tool,
    describe,
    footprint,
    tool_definitions,
)
from slurm_mcp.topics import TOPICS, topic_names

README = Path(__file__).resolve().parent.parent / "README.md"


def test_exactly_three_tools_are_exposed() -> None:
    """The whole design claim. If this grows, the README claim is stale."""
    names = [t["name"] for t in tool_definitions()]
    assert names == ["slurm_overview", "slurm_query", "slurm_describe"]


def test_tool_descriptors_are_valid_json_and_enumerate_topics() -> None:
    defs = tool_definitions()
    json.dumps(defs)
    query = next(d for d in defs if d["name"] == "slurm_query")
    assert query["inputSchema"]["properties"]["topic"]["enum"] == topic_names()


def test_every_tool_is_annotated_read_only_and_closed() -> None:
    """readOnlyHint defaults to false in the MCP schema, i.e. "may write"."""
    for d in tool_definitions():
        assert d["annotations"]["readOnlyHint"] is True
        assert d["annotations"]["destructiveHint"] is False
        assert d["inputSchema"]["additionalProperties"] is False


def test_every_topic_has_detail_worth_fetching() -> None:
    """slurm_describe is only justified if there is something behind it."""
    for name in topic_names():
        assert len(TOPICS[name].detail) > 120, name
        assert name in describe(name)


def test_describe_prints_the_column_legend() -> None:
    assert "columns: account|user|rawshares|normshares|rawusage|effectvusage|fairshare" in (
        describe("fairshare")
    )
    assert "columns: jobid|account|user|state" in describe("accounting")
    assert "columns:" not in describe("config")


# --- input validation and isError -----------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"topic": "queue", "filters": "notadict"},
        {"topic": "queue", "filters": ["a", "b"]},
        {"topic": "queue", "filters": {"user": 123}},
        {"topic": "queue", "filters": {"user": None}},
        {"topic": "queue", "surprise": 1},
        {"topic": "nope"},
        {"topic": 7},
        {},
    ],
)
def test_malformed_query_arguments_are_tool_errors_not_crashes(arguments: dict[str, Any]) -> None:
    reply = call_tool("slurm_query", arguments, use_fixtures=True)
    assert reply.is_error
    assert reply.text.startswith("error:")


def test_non_object_arguments_are_a_tool_error() -> None:
    assert call_tool("slurm_query", ["queue"]).is_error
    assert call_tool("slurm_overview", "x").is_error


def test_overview_takes_no_arguments() -> None:
    assert call_tool("slurm_overview", {"verbose": True}, use_fixtures=True).is_error


def test_injection_through_a_filter_is_a_tool_error() -> None:
    reply = call_tool(
        "slurm_query",
        {"topic": "priority", "filters": {"user": "--format=$(id)"}},
        use_fixtures=True,
    )
    assert reply.is_error
    assert "refused" in reply.text


def test_a_successful_query_is_not_an_error() -> None:
    reply = call_tool("slurm_query", {"topic": "queue", "filters": None}, use_fixtures=True)
    assert not reply.is_error
    assert reply.text.startswith("[fixture]")


def test_a_failed_live_read_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    reply = call_tool("slurm_query", {"topic": "queue"})
    assert reply.is_error
    assert "squeue not found" in reply.text
    overview = call_tool("slurm_overview", {})
    assert overview.is_error and overview.text.count("not found") == 3


def test_describe_validates_its_topic() -> None:
    assert call_tool("slurm_describe", {"topic": "nope"}).is_error
    assert not call_tool("slurm_describe", {"topic": "config"}).is_error


def test_unknown_tool_is_reported_not_raised() -> None:
    reply = call_tool("slurm_delete_everything", {})
    assert reply.is_error and "unknown tool" in reply.text


# --- the footprint measurement --------------------------------------------


def test_progressive_disclosure_is_cheaper_under_the_stated_assumption() -> None:
    """Guards the direction of the README claim, and only the claim it makes.

    The three-tool surface is cheaper than a flat one *if* a flat server would
    inline guidance comparable to slurm_describe. Schemas alone are roughly the
    same size as the resident surface, and the README says so.
    """
    f = footprint()
    assert f["resident_total"] == f["resident_tool_list"] + len(INSTRUCTIONS)
    assert f["resident_total"] < f["flat_with_inlined_detail"]
    assert f["flat_with_inlined_detail"] == f["flat_schemas_only"] + f["on_request_detail"]
    assert f["flat_binaries"] == len({t.argv[0] for t in TOPICS.values()})


def test_readme_footprint_numbers_match_the_measurement() -> None:
    """The README block is `slurm-mcp footprint` output; it must not go stale."""
    text = README.read_text(encoding="utf-8")
    f = footprint()
    for key, label in [
        ("resident_tool_list", "resident: three tool descriptors"),
        ("resident_instructions", "resident: server instructions"),
        ("resident_total", "resident total"),
        ("on_request_detail", "detail, fetched on request"),
        ("flat_schemas_only", "schemas only"),
        ("flat_with_inlined_detail", "schemas + inlined detail"),
    ]:
        match = re.search(re.escape(label) + r"\s+(\d+) chars", text)
        assert match, label
        assert int(match.group(1)) == f[key], label
