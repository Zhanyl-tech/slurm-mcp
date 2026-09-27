from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slurm_mcp.execute import read_fixture
from slurm_mcp.guard import Denied, check_binary_shape, guard, is_allowed
from slurm_mcp.topics import TOPICS, InvalidFilter, build_argv, topic_names


def test_every_topic_builds_a_permitted_command() -> None:
    """The closed vocabulary must not be able to express a denied command."""
    for name in topic_names():
        assert is_allowed(build_argv(name)), name


def test_every_topic_filter_builds_a_permitted_command() -> None:
    for name, topic in TOPICS.items():
        for key in topic.filters:
            assert is_allowed(build_argv(name, {key: "alice"})), f"{name}.{key}"


def test_the_binary_allowlist_covers_every_topic_option() -> None:
    """Layer 1 is hand-written; this is what keeps it in step with the topics."""
    for name, topic in TOPICS.items():
        check_binary_shape(build_argv(name, dict.fromkeys(topic.filters, "x")))


@pytest.mark.parametrize(
    "value",
    [
        "; rm -rf /",
        "--format=$(id)",
        "-t",
        "a\x00b",
        "alice\nscancel",
        "alice bob",
        "",
        "$(id)",
        "x" * 129,
        "ålice",
    ],
)
def test_filter_values_cannot_smuggle_a_command(value: str) -> None:
    with pytest.raises(InvalidFilter):
        build_argv("queue", {"user": value})
    # ...and if something bypassed build_argv, the guard refuses the same value.
    with pytest.raises(Denied):
        guard(build_argv("queue") + ["-u", value])


@pytest.mark.parametrize(
    "value", ["alice", "gpu,cpu", "PENDING", "2026-08-01T00:00:00", "now-1days", "a.b@site", "1001"]
)
def test_ordinary_filter_values_are_accepted(value: str) -> None:
    guard(build_argv("accounting", {"user": value}))


def test_unknown_topic_and_filter_raise() -> None:
    with pytest.raises(KeyError):
        build_argv("nope")
    with pytest.raises(KeyError):
        build_argv("queue", {"nope": "x"})


def test_tabular_topics_declare_their_columns() -> None:
    """Every pipe-delimited topic says what its columns are (slurm_describe prints them)."""
    for name, topic in TOPICS.items():
        if name in ("diagnostics", "config"):
            assert not topic.columns
            continue
        assert topic.columns, name
        for key in ("user", "partition", "account"):
            if key in topic.filters:
                assert key in topic.columns, f"{name}: filter {key} has no column"


def test_fixtures_have_the_declared_column_count() -> None:
    """The hand-written fixtures match the shape their topic's command would print."""
    for name, topic in TOPICS.items():
        text = read_fixture(name)
        assert text, name
        if not topic.columns:
            continue
        for line in text.splitlines():
            assert len(line.split("|")) == len(topic.columns), (name, line)


def test_priority_labels_partition_name_and_weight_separately() -> None:
    """%r is the partition name; %P is the weighted partition priority (sprio(1))."""
    topic = TOPICS["priority"]
    fmt = topic.argv[topic.argv.index("-o") + 1].split("|")
    assert fmt[topic.columns.index("partition")] == "%r"
    assert fmt[topic.columns.index("partition_prio")] == "%P"


FILTERABLE = [(name, key) for name, t in TOPICS.items() for key in t.filters]


@settings(max_examples=300, deadline=None)
@given(pair=st.sampled_from(FILTERABLE), value=st.text(max_size=40))
def test_property_build_argv_is_either_refused_or_exactly_prefix_plus_pair(
    pair: tuple[str, str], value: str
) -> None:
    """For any value: refused up front, or the argv is the topic plus one pair
    and the guard accepts that exact shape. Nothing in between."""
    name, key = pair
    topic = TOPICS[name]
    try:
        argv = build_argv(name, {key: value})
    except InvalidFilter:
        assert not is_allowed(list(topic.argv) + [topic.filters[key], value])
        return
    assert argv == list(topic.argv) + [topic.filters[key], value]
    guard(argv)


@settings(max_examples=300, deadline=None)
@given(
    name=st.sampled_from(topic_names()),
    tail=st.lists(
        st.one_of(
            st.sampled_from(["-u", "-p", "-t", "-S", "-E", "-s", "-A", "-o", "-r", "--reset"]),
            st.text(max_size=12),
        ),
        max_size=6,
    ),
)
def test_property_the_guard_accepts_only_topic_shapes(name: str, tail: list[str]) -> None:
    """Whatever follows a topic's command, the guard accepts it only as that
    topic's own flag/value pairs with plain values."""
    topic = TOPICS[name]
    argv = list(topic.argv) + tail
    if not is_allowed(argv):
        return
    assert len(tail) % 2 == 0
    flags = set(topic.filters.values())
    for flag, value in zip(tail[::2], tail[1::2], strict=True):
        assert flag in flags
        assert not value.startswith("-")
        assert value.isprintable() and " " not in value
