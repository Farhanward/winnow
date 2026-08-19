"""Tests for the read ledger - the input half of the bill.

39% of the Read calls measured on the development machine asked for a file the
same session had already been given. These cover the suppression, and more
importantly every case where suppression must not happen: a changed file, a
different range, a different session, a second insistent request, and a
compaction that invalidates everything the ledger believed.
"""

import json
import time

import pytest

from winnow import config, hook, reads


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNOW_HOME", str(tmp_path / "wh"))
    return tmp_path


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("line\n" * 200, encoding="utf-8")
    return path


def _read(capsys, path, session="s1", **extra):
    """Run the hook on one Read and return (updatedInput, additionalContext)."""
    tool_input = {"file_path": str(path)}
    tool_input.update(extra)
    event = json.dumps(
        {"tool_name": "Read", "tool_input": tool_input, "session_id": session}
    )
    assert hook.run_hook(event) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return None, None
    body = json.loads(out)["hookSpecificOutput"]
    return body.get("updatedInput"), body.get("additionalContext")


def _compact(capsys, session="s1", stage="PreCompact"):
    event = json.dumps({"hook_event_name": stage, "session_id": session})
    assert hook.run_hook(event) == 0
    capsys.readouterr()


def test_the_first_read_is_served_untouched(isolated_home, source, capsys):
    assert _read(capsys, source) == (None, None)


def test_an_immediate_re_read_is_cut_to_a_stub(isolated_home, source, capsys):
    _read(capsys, source)
    updated, note = _read(capsys, source)

    assert updated["limit"] == 1
    assert "already read" in note
    # The model must be told the content it holds is still current, and how to
    # get the file back if it is not.
    assert "has not changed" in note
    assert "again" in note


def test_asking_twice_serves_the_file(isolated_home, source, capsys):
    """Context may have been compacted since. Insisting is always honoured."""
    _read(capsys, source)
    _read(capsys, source)

    assert _read(capsys, source) == (None, None)


def test_a_changed_file_is_never_suppressed(isolated_home, source, capsys):
    _read(capsys, source)
    time.sleep(0.01)
    source.write_text("different content\n", encoding="utf-8")

    assert _read(capsys, source) == (None, None)


def test_a_different_range_is_a_different_read(isolated_home, source, capsys):
    _read(capsys, source)

    assert _read(capsys, source, offset=100) == (None, None)


def test_another_session_holds_nothing(isolated_home, source, capsys):
    _read(capsys, source, session="one")

    assert _read(capsys, source, session="two") == (None, None)


def test_compaction_makes_the_ledger_forget(isolated_home, source, capsys):
    """After compaction the model may no longer hold what it was given."""
    _read(capsys, source)
    _compact(capsys)

    assert _read(capsys, source) == (None, None)


def test_session_end_also_clears_the_ledger(isolated_home, source, capsys):
    _read(capsys, source)
    _compact(capsys, stage="SessionEnd")

    assert _read(capsys, source) == (None, None)


def test_dedupe_can_be_turned_off(isolated_home, source, capsys):
    (config.home() / "config.json").write_text(
        json.dumps({"dedupe_reads": False}), encoding="utf-8"
    )
    _read(capsys, source)

    assert _read(capsys, source) == (None, None)


def test_a_read_without_a_session_is_never_suppressed(isolated_home, source, capsys):
    """Codex sends no session id, and a shared bucket would cross the wires."""
    _read(capsys, source, session="")

    assert _read(capsys, source, session="") == (None, None)


def test_an_aged_out_record_is_served_again(isolated_home, source, capsys):
    _read(capsys, source)
    ledger = reads.Ledger()
    ledger.conn.execute("UPDATE reads SET served_at = served_at - 100000")
    ledger.conn.commit()
    ledger.close()

    assert _read(capsys, source) == (None, None)


def test_a_vanished_file_is_left_to_the_tool(isolated_home, tmp_path, capsys):
    """Reporting a missing file is the tool's job, not the hook's."""
    missing = tmp_path / "gone.py"

    assert _read(capsys, missing) == (None, None)


def test_the_stamp_notices_a_size_change(isolated_home, source):
    before = reads.Ledger.stamp(str(source))
    source.write_text("x", encoding="utf-8")

    assert reads.Ledger.stamp(str(source)) != before


def test_the_key_separates_ranges_of_the_same_file():
    a = reads.Ledger.key("/tmp/f.py", None, None)
    b = reads.Ledger.key("/tmp/f.py", 100, None)

    assert a != b


def test_elapsed_reads_as_a_sentence_fragment():
    assert reads.elapsed(5) == "moments ago"
    assert reads.elapsed(600) == "10 minutes ago"
    assert reads.elapsed(3600) == "1 hour ago"
    assert reads.elapsed(7200) == "2 hours ago"


def test_hook_install_writes_the_compaction_events(isolated_home, tmp_path):
    from winnow import cli

    target = tmp_path / "settings.json"
    cli._hook_install(str(target))
    written = json.loads(target.read_text(encoding="utf-8"))

    assert set(written["hooks"]) == {"PreToolUse", "PreCompact", "SessionEnd"}
    assert len(written["hooks"]["PreToolUse"]) == 6

    # Installing twice must not double anything.
    cli._hook_install(str(target))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert len(written["hooks"]["PreToolUse"]) == 6
    assert len(written["hooks"]["PreCompact"]) == 1


def test_the_reads_command_reports_and_clears(isolated_home, source, capsys):
    """A suppression the user cannot inspect is one they cannot trust."""
    from argparse import Namespace

    from winnow import cli

    _read(capsys, source)
    cli.cmd_reads(Namespace(action="stats", limit=20, json=False))
    out = capsys.readouterr().out
    assert "1 remembered" in out
    assert "module.py" in out

    cli.cmd_reads(Namespace(action="clear", limit=20, json=False))
    assert "cleared 1" in capsys.readouterr().out

    cli.cmd_reads(Namespace(action="stats", limit=20, json=False))
    assert "empty" in capsys.readouterr().out
