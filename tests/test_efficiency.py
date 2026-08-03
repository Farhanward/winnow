"""Tests for the privacy-preserving runtime efficiency collector."""

import json
import os
import sqlite3
from argparse import Namespace

import pytest

from winnow import cli, efficiency, hook


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNOW_HOME", str(tmp_path / "wh"))
    return tmp_path


def test_detect_client_prefers_explicit_and_known_runtime_markers():
    assert efficiency.detect_client("local", {"CODEX_THREAD_ID": "x"}) == "local"
    assert efficiency.detect_client(None, {"CODEX_THREAD_ID": "x"}) == "codex"
    assert efficiency.detect_client(None, {"CLAUDECODE": "1"}) == "claude"
    assert efficiency.detect_client(None, {}) == "unknown"


def test_collector_ignores_unidentified_runtime(isolated_home):
    collector = efficiency.Collector()
    collector.observe("unknown", selected=True)
    collector.record_run("unknown", 100, 10, True, 1_000_000)
    rows = collector.snapshot()
    count = collector.conn.execute(
        "SELECT COUNT(*) FROM runtime_efficiency"
    ).fetchone()[0]
    collector.close()

    assert count == 0
    assert all(row.runs == 0 for row in rows.values())


def test_collector_aggregates_numbers_without_payload(isolated_home):
    collector = efficiency.Collector()
    collector.observe("codex", selected=True)
    collector.record_run(
        "codex",
        raw_tokens=100,
        output_tokens=25,
        compressed=True,
        process_ns=2_000_000,
    )
    collector.record_run(
        "codex",
        raw_tokens=20,
        output_tokens=20,
        compressed=False,
        process_ns=1_000_000,
    )
    row = collector.snapshot()["codex"]
    collector.close()

    assert row.observed == 1
    assert row.selected == 1
    assert row.runs == 2
    assert row.compressed == 1
    assert row.passthrough == 1
    assert row.raw_tokens == 120
    assert row.output_tokens == 45
    assert row.saved_tokens == 75
    assert row.reduction_pct == 62.5
    assert row.average_process_ms == 1.5

    db = sqlite3.connect(efficiency.metrics_path())
    columns = {
        item[1] for item in db.execute("PRAGMA table_info(runtime_efficiency)")
    }
    db.close()
    assert "command" not in columns
    assert "cwd" not in columns
    assert "raw" not in columns


def test_runtimes_remain_independent_across_long_gaps(
    isolated_home, monkeypatch
):
    monkeypatch.setattr(efficiency.time, "time", lambda: 1_000.0)
    collector = efficiency.Collector()
    collector.record_run("codex", 100, 25, True, 1_000_000)
    collector.close()

    monkeypatch.setattr(efficiency.time, "time", lambda: 7_777_000.0)
    collector = efficiency.Collector()
    collector.record_run("local", 40, 40, False, 1_000_000)
    rows = collector.snapshot()
    collector.close()

    assert rows["codex"].runs == 1
    assert rows["codex"].updated_at == 1_000.0
    assert rows["local"].runs == 1
    assert rows["local"].updated_at == 7_777_000.0
    assert rows["claude"].runs == 0
    assert rows["claude"].updated_at == 0.0


def test_hook_tags_claude_and_records_selection(
    isolated_home, capsys, monkeypatch
):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    event = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    })

    assert hook.run_hook(event) == 0
    decision = json.loads(capsys.readouterr().out)
    updated = decision["hookSpecificOutput"]["updatedInput"]["command"]
    assert updated == "wn run --client claude -- git status"

    collector = efficiency.Collector()
    row = collector.snapshot()["claude"]
    collector.close()
    assert row.observed == 1
    assert row.selected == 1


def test_hook_accepts_namespaced_codex_shell_tool(
    isolated_home, capsys, monkeypatch
):
    monkeypatch.setenv("CODEX_THREAD_ID", "test-thread")
    event = json.dumps({
        "tool_name": "functions.shell_command",
        "tool_input": {"command": "git status"},
    })

    assert hook.run_hook(event) == 0
    decision = json.loads(capsys.readouterr().out)
    updated = decision["hookSpecificOutput"]["updatedInput"]["command"]
    assert "--client codex" in updated


def test_run_records_passthrough_even_with_no_remember(
    isolated_home, capsys, monkeypatch
):
    monkeypatch.setenv("CODEX_THREAD_ID", "test-thread")
    args = Namespace(
        rest=["--", os.sys.executable, "-c", "print('small output')"],
        shell=False,
        no_remember=True,
        no_footer=True,
        client=None,
    )

    assert cli.cmd_run(args) == 0
    capsys.readouterr()

    collector = efficiency.Collector()
    row = collector.snapshot()["codex"]
    collector.close()
    assert row.runs == 1
    assert row.passthrough == 1
    assert row.raw_tokens > 0
    assert row.raw_tokens == row.output_tokens


def test_efficiency_command_reports_all_three_clients(isolated_home, capsys):
    collector = efficiency.Collector()
    collector.record_run("codex", 100, 20, True, 1_000_000)
    collector.record_run("claude", 100, 50, True, 1_000_000)
    collector.record_run("local", 100, 100, False, 1_000_000)
    collector.close()

    assert cli.cmd_efficiency(Namespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "codex" in out
    assert "claude" in out
    assert "local" in out
    assert "80.0%" in out


def test_efficiency_command_marks_unused_runtime_as_no_data(
    isolated_home, capsys
):
    collector = efficiency.Collector()
    collector.record_run("codex", 100, 20, True, 1_000_000)
    collector.close()

    assert cli.cmd_efficiency(Namespace(json=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    claude = next(line for line in lines if line.startswith("claude"))
    local = next(line for line in lines if line.startswith("local"))
    assert "no data" in claude
    assert "no data" in local
