"""Tests for the agent-side token audit.

The fixtures here are hand-written transcript lines in the shape Claude Code
writes: one JSON object per line, usage counters on assistant messages, tool
results attached to the following user message.
"""

import json

import pytest

from winnow import agent


def _assistant(session, usage, content=None, sidechain=False, mcp=None):
    entry = {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-08-19T10:00:00Z",
        "isSidechain": sidechain,
        "message": {"model": "claude-opus-5", "usage": usage, "content": content or []},
    }
    if mcp:
        entry["attributionMcpServer"] = mcp
    return entry


def _usage(inp=0, out=0, create=0, read=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read,
    }


def _write(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def transcript(tmp_path):
    entries = [
        _assistant("s1", _usage(inp=10, out=100, create=50_000, read=0)),
        _assistant("s1", _usage(inp=5, out=200, read=50_000)),
        _assistant("s1", _usage(inp=5, out=300, read=70_000)),
        # A subagent request. Its floor belongs to the subagent, not to s1.
        _assistant("s1", _usage(inp=1, out=50, read=9_000), sidechain=True),
    ]
    return _write(tmp_path, entries)


def test_scan_totals_and_floor(transcript):
    report = agent.scan(paths=[transcript])

    assert report.files == 1
    assert report.requests == 4
    assert report.subagent_requests == 1
    assert report.totals["output_tokens"] == 650
    assert report.context_read == 129_000
    # The floor is the smallest whole prompt any main-thread request paid:
    # request two, at 5 fresh input tokens on top of a 50,000-token prefix.
    assert report.floor == 50_005
    assert report.fixed_cost == 50_005 * 4


def test_subagent_floor_never_lowers_the_session_floor(transcript):
    report = agent.scan(paths=[transcript])
    session = report.sessions[0]

    assert session.requests == 3
    assert session.floor == 50_005
    assert report.subagent_totals["cache_read_input_tokens"] == 9_000


def test_tool_calls_and_result_sizes_are_attributed(tmp_path):
    entries = [
        _assistant(
            "s2",
            _usage(inp=1, out=10, read=1_000),
            content=[{"type": "tool_use", "id": "t1", "name": "Read"}],
        ),
        {
            "type": "user",
            "sessionId": "s2",
            "toolUseResult": {"file": {"content": "x" * 500}},
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1"}]
            },
        },
        _assistant(
            "s2",
            _usage(inp=1, out=10, read=1_000),
            content=[{"type": "tool_use", "id": "t2", "name": "mcp__figma__get_file"}],
            mcp="figma",
        ),
    ]
    report = agent.scan(paths=[_write(tmp_path, entries)])

    assert report.tool_calls["Read"] == 1
    assert report.tool_calls["mcp__figma__get_file"] == 1
    assert report.tool_result_bytes["Read"] > 500
    assert report.mcp_calls["figma"] == 1


def test_days_window_skips_older_files(tmp_path, monkeypatch):
    path = _write(tmp_path, [_assistant("s3", _usage(inp=1, out=1, read=10))])
    import os
    import time

    old = time.time() - 40 * 86400
    os.utime(path / "session.jsonl", (old, old))

    assert agent.scan(paths=[path], days=7).files == 0
    assert agent.scan(paths=[path]).files == 1


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps(_assistant("s4", _usage(inp=1, out=1, read=10)))
        + "\n[]\n",
        encoding="utf-8",
    )

    report = agent.scan(paths=[tmp_path])
    assert report.requests == 1


def test_namespace_reads_the_server_out_of_a_tool_name():
    assert agent.namespace("mcp__figma__get_file") == "figma"
    assert agent.namespace("Bash") is None
    assert agent.namespace("mcp__broken") is None


def test_server_usage_folds_spelling_variants(tmp_path, monkeypatch):
    entries = [
        _assistant(
            "s5",
            _usage(inp=1, out=1, read=10),
            content=[{"type": "tool_use", "id": "a", "name": "mcp__claude_browser__go"}],
            mcp="Claude Browser",
        ),
    ]
    monkeypatch.setattr(agent, "configured_servers", lambda: {"unused@vendor": "cfg"})
    report = agent.scan(paths=[_write(tmp_path, entries)])
    used, idle = agent.server_usage(report)

    # One server, not two, even though it was attributed under two spellings.
    assert len(used) == 1
    assert sum(used.values()) == 2
    assert idle == ["unused@vendor"]


def test_configured_server_matches_its_plugin_suffix(tmp_path, monkeypatch):
    entries = [
        _assistant(
            "s6",
            _usage(inp=1, out=1, read=10),
            content=[{"type": "tool_use", "id": "a", "name": "mcp__figma__ping"}],
        ),
    ]
    monkeypatch.setattr(
        agent, "configured_servers", lambda: {"figma@claude-plugins-official": "cfg"}
    )
    report = agent.scan(paths=[_write(tmp_path, entries)])
    _, idle = agent.server_usage(report)

    assert idle == []


def test_empty_transcript_directory_reports_nothing(tmp_path):
    report = agent.scan(paths=[tmp_path])

    assert report.files == 0
    assert report.floor == 0
    assert report.fixed_cost == 0
    assert report.subagent_share == 0.0
