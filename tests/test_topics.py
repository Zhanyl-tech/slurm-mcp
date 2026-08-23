from __future__ import annotations

import json

import pytest

from slurm_mcp.execute import run_topic
from slurm_mcp.guard import is_allowed
from slurm_mcp.server import call_tool, describe, footprint, tool_definitions
from slurm_mcp.topics import TOPICS, build_argv, topic_names


def test_every_topic_builds_a_permitted_command() -> None:
    """The closed vocabulary must not be able to express a denied command."""
    for name in topic_names():
        assert is_allowed(build_argv(name)), name


def test_every_topic_filter_builds_a_permitted_command() -> None:
    for name, topic in TOPICS.items():
        for key in topic.filters:
            assert is_allowed(build_argv(name, {key: "alice"})), f"{name}.{key}"


def test_filter_values_cannot_smuggle_a_command() -> None:
    argv = build_argv("queue", {"user": "; rm -rf /"})
    assert not is_allowed(argv)


def test_unknown_topic_and_filter_raise() -> None:
    with pytest.raises(KeyError):
        build_argv("nope")
    with pytest.raises(KeyError):
        build_argv("queue", {"nope": "x"})


def test_exactly_three_tools_are_exposed() -> None:
    """The whole design claim. If this grows, the README claim is stale."""
    names = [t["name"] for t in tool_definitions()]
    assert names == ["slurm_overview", "slurm_query", "slurm_describe"]


def test_tool_descriptors_are_valid_json_and_enumerate_topics() -> None:
    defs = tool_definitions()
    json.dumps(defs)
    query = next(d for d in defs if d["name"] == "slurm_query")
    assert query["inputSchema"]["properties"]["topic"]["enum"] == topic_names()


def test_every_topic_has_detail_worth_fetching() -> None:
    """slurm_describe is only justified if there is something behind it."""
    for name in topic_names():
        assert len(TOPICS[name].detail) > 120, name
        assert name in describe(name)


def test_progressive_disclosure_is_actually_cheaper() -> None:
    """Guards the README's measured claim."""
    f = footprint()
    assert f["resident_three_tools"] < f["flat_surface_always_resident"]
    assert f["flat_surface_always_resident"] / f["resident_three_tools"] > 2.0


def test_fixture_mode_serves_every_topic() -> None:
    for name in topic_names():
        r = run_topic(name, use_fixtures=True)
        assert r.source == "fixture"
        assert r.stdout.strip(), name


def test_responses_always_state_their_source() -> None:
    """A fixture must never be mistakable for a live read."""
    out = run_topic("queue", use_fixtures=True).render()
    assert out.startswith("[fixture]")


def test_overview_covers_the_three_opening_questions() -> None:
    out = call_tool("slurm_overview", {}, use_fixtures=True)
    for expected in ("nodes", "queue", "diagnostics"):
        assert f"### {expected}" in out


def test_unknown_tool_is_reported_not_raised() -> None:
    assert "unknown tool" in call_tool("slurm_delete_everything", {})
