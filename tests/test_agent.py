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


# --------------------------------------------------------------------------- #
# Codex rollouts
# --------------------------------------------------------------------------- #
def _codex_tokens(prompt, cached, out, reasoning=0):
    return {
        "timestamp": "2026-08-19T10:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": prompt,
                    "cached_input_tokens": cached,
                    "output_tokens": out,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": prompt + out,
                }
            },
        },
    }


def test_codex_rollout_is_read_and_not_double_counted(tmp_path):
    """Codex input_tokens already contains the cached part.

    Adding the two the way Claude's columns add would count the prefix twice,
    so the prompt total must come from input_tokens alone.
    """
    entries = [
        {"type": "session_meta", "payload": {"id": "abc", "cwd": "/tmp"}},
        _codex_tokens(prompt=40_000, cached=30_000, out=700, reasoning=500),
        _codex_tokens(prompt=52_000, cached=45_000, out=300),
    ]
    report = agent.scan(paths=[_write(tmp_path, entries)])
    codex = report.runtimes[agent.CODEX]

    assert codex.requests == 2
    assert codex.floor == 40_000
    assert codex.context_read == 75_000
    assert codex.totals["input_tokens"] == 10_000 + 7_000
    # Reasoning output is output the model was billed for, so it is counted.
    assert codex.totals["output_tokens"] == 1_500


def test_codex_tool_calls_are_attributed(tmp_path):
    entries = [
        {"type": "session_meta", "payload": {"id": "abc"}},
        {"type": "response_item",
         "payload": {"type": "function_call", "name": "shell_command"}},
        {"type": "response_item",
         "payload": {"type": "function_call_output", "output": "x" * 300}},
    ]
    report = agent.scan(paths=[_write(tmp_path, entries)])

    assert report.tool_calls["shell_command"] == 1
    assert report.tool_result_bytes["codex tool output"] == 300


# --------------------------------------------------------------------------- #
# Gemini in Antigravity
# --------------------------------------------------------------------------- #
def test_gemini_steps_are_counted_and_tokens_reported_unknown(tmp_path):
    """Antigravity records no usage counters, and a zero would read as free."""
    entries = [
        {"step_index": 0, "created_at": "2026-08-19T10:00:00Z",
         "type": "USER_INPUT", "status": "DONE", "content": "hi"},
        {"step_index": 1, "created_at": "2026-08-19T10:00:01Z",
         "type": "PLANNER_RESPONSE", "status": "DONE",
         "tool_calls": [{"tool_name": "run_command"}]},
    ]
    report = agent.scan(paths=[_write(tmp_path, entries)])
    gemini = report.runtimes[agent.GEMINI]

    assert gemini.tokens_available is False
    assert gemini.steps == 2
    assert gemini.requests == 0
    assert report.tool_calls["run_command"] == 1
    # No token figure was invented for a runtime that reports none.
    assert gemini.totals == {}


def test_antigravity_duplicate_transcript_is_read_once(tmp_path):
    step = {"step_index": 0, "created_at": "2026-08-19T10:00:00Z",
            "type": "PLANNER_RESPONSE", "status": "DONE",
            "tool_calls": [{"tool_name": "view_file"}]}
    line = json.dumps(step) + "\n"
    (tmp_path / "transcript.jsonl").write_text(line, encoding="utf-8")
    (tmp_path / "transcript_full.jsonl").write_text(line, encoding="utf-8")

    report = agent.scan(paths=[tmp_path])

    assert report.runtimes[agent.GEMINI].files == 1
    assert report.tool_calls["view_file"] == 1


# --------------------------------------------------------------------------- #
# mixed roots
# --------------------------------------------------------------------------- #
def test_runtimes_are_kept_apart_and_cost_is_summed_per_runtime(tmp_path):
    """Claude and Codex sit at different floors.

    A blended median times the combined request count would report a fixed
    cost neither runtime paid, so the total is summed per runtime instead.
    """
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    claude_dir.mkdir()
    codex_dir.mkdir()
    _write(claude_dir, [
        _assistant("s1", _usage(inp=10, out=50, read=60_000)),
        _assistant("s1", _usage(inp=10, out=50, read=60_000)),
    ])
    _write(codex_dir, [
        {"type": "session_meta", "payload": {"id": "c"}},
        _codex_tokens(prompt=10_000, cached=8_000, out=100),
    ])

    report = agent.scan(paths=[claude_dir, codex_dir])

    assert set(report.runtimes) == {agent.CLAUDE, agent.CODEX}
    assert report.runtimes[agent.CLAUDE].floor == 60_010
    assert report.runtimes[agent.CODEX].floor == 10_000
    assert report.fixed_cost == 60_010 * 2 + 10_000 * 1


def test_detect_names_the_runtime_from_the_line_shape():
    assert agent.detect({"type": "session_meta", "payload": {}}) == agent.CODEX
    assert agent.detect({"step_index": 3, "created_at": "x"}) == agent.GEMINI
    assert agent.detect({"type": "assistant", "sessionId": "s"}) == agent.CLAUDE
    assert agent.detect({"nothing": "useful"}) is None


def test_unrecognised_file_is_counted_not_parsed(tmp_path):
    (tmp_path / "other.jsonl").write_text(
        json.dumps({"some": "other tool"}) + "\n", encoding="utf-8"
    )
    report = agent.scan(paths=[tmp_path])

    assert report.files == 0
    assert report.unrecognised == 1
