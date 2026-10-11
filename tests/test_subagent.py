"""Tests for the subagent brief.

A subagent starts cold, pays its own floor on every request it makes, and
returns a report that lands in the parent's context and stays there. The brief
is the one lever a PreToolUse hook has over any of that, and it costs tokens
itself, so the tests care as much about when it stays out of the way.
"""

import json

import pytest

from winnow import config, hook


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNOW_HOME", str(tmp_path / "wh"))
    return tmp_path


def _spawn(capsys, tool_input, tool="Agent"):
    event = json.dumps({"tool_name": tool, "tool_input": tool_input})
    assert hook.run_hook(event) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]


def test_a_subagent_prompt_is_briefed(isolated_home, capsys):
    updated = _spawn(capsys, {"prompt": "Find the call sites of parse_config."})

    assert updated["prompt"].startswith("Find the call sites of parse_config.")
    assert "Token budget (winnow):" in updated["prompt"]
    assert "offset and limit" in updated["prompt"]
    assert "under 200 words" in updated["prompt"]


def test_the_original_prompt_is_never_altered(isolated_home, capsys):
    """The brief is appended. Rewriting the caller's words would change the task."""
    original = "Refactor the parser.\n\nKeep the public API stable."
    updated = _spawn(capsys, {"prompt": original})

    assert updated["prompt"].startswith(original)


def test_every_other_parameter_survives(isolated_home, capsys):
    updated = _spawn(
        capsys,
        {"prompt": "go", "subagent_type": "Explore", "description": "scan"},
    )

    assert updated["subagent_type"] == "Explore"
    assert updated["description"] == "scan"


def test_both_names_of_the_subagent_tool_are_covered(isolated_home, capsys):
    """The tool has been called Task and Agent across versions."""
    assert _spawn(capsys, {"prompt": "go"}, tool="Task") is not None
    assert _spawn(capsys, {"prompt": "go"}, tool="Agent") is not None


def test_codex_spawn_defaults_to_no_fork(isolated_home, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")

    updated = _spawn(capsys, {"message": "go"}, tool="spawn_agent")

    assert updated["fork_turns"] == "none"
    assert "Token budget (winnow):" in updated["message"]


def test_codex_spawn_preserves_explicit_fork(isolated_home, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")

    updated = _spawn(
        capsys, {"message": "go", "fork_turns": "all"}, tool="spawn_agent"
    )

    assert updated["fork_turns"] == "all"


def test_claude_agent_schema_has_no_fork_field(isolated_home, capsys):
    updated = _spawn(capsys, {"prompt": "go"}, tool="Agent")

    assert "fork_turns" not in updated


def test_namespaced_codex_spawn_is_briefed(isolated_home, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")

    updated = _spawn(
        capsys, {"message": "go"}, tool="mcp__collaboration__spawn_agent"
    )

    assert updated["fork_turns"] == "none"


def test_briefing_twice_is_refused(isolated_home, capsys):
    once = _spawn(capsys, {"prompt": "go"})

    assert _spawn(capsys, {"prompt": once["prompt"]}) is None


def test_a_prompt_that_already_carries_a_budget_is_left_alone(isolated_home, capsys):
    written_by_hand = "Do the thing.\n\nToken budget (winnow): be brief."

    assert _spawn(capsys, {"prompt": written_by_hand}) is None


def test_codex_prebriefed_message_still_defaults_to_no_fork(
    isolated_home, capsys, monkeypatch
):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    message = "go\n\nToken budget (winnow): already present"

    updated = _spawn(capsys, {"message": message}, tool="spawn_agent")

    assert updated == {"message": message, "fork_turns": "none"}


def test_a_call_without_a_prompt_is_left_alone(isolated_home, capsys):
    assert _spawn(capsys, {"description": "no prompt here"}) is None
    assert _spawn(capsys, {"prompt": "   "}) is None


def test_the_brief_can_be_turned_off(isolated_home, capsys):
    (config.home() / "config.json").write_text(
        json.dumps({"subagent_budget": False}), encoding="utf-8"
    )

    assert _spawn(capsys, {"prompt": "go"}) is None


def test_disabled_brief_still_defaults_codex_fork(isolated_home, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    (config.home() / "config.json").write_text(
        json.dumps({"subagent_budget": False}), encoding="utf-8"
    )

    assert _spawn(capsys, {"message": "go"}, tool="spawn_agent") == {
        "message": "go",
        "fork_turns": "none",
    }


def test_disabled_brief_does_not_mutate_explicit_codex_fork(
    isolated_home, capsys, monkeypatch
):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    (config.home() / "config.json").write_text(
        json.dumps({"subagent_budget": False}), encoding="utf-8"
    )

    assert _spawn(
        capsys, {"message": "go", "fork_turns": "all"}, tool="spawn_agent"
    ) is None


def test_a_zero_word_ceiling_drops_only_that_line(isolated_home, capsys):
    (config.home() / "config.json").write_text(
        json.dumps({"subagent_report_words": 0}), encoding="utf-8"
    )
    updated = _spawn(capsys, {"prompt": "go"})

    assert "Report in under" not in updated["prompt"]
    assert "offset and limit" in updated["prompt"]


def test_an_unreadable_setting_never_costs_a_subagent(isolated_home, capsys):
    """A hand-edited config must degrade, not block the spawn."""
    (config.home() / "config.json").write_text(
        json.dumps({"subagent_report_words": "many"}), encoding="utf-8"
    )
    updated = _spawn(capsys, {"prompt": "go"})

    assert updated is not None
    assert "under 200 words" in updated["prompt"]


def test_the_brief_stays_short(isolated_home, capsys):
    """It is paid once per subagent, so it has to earn its own length back."""
    updated = _spawn(capsys, {"prompt": "go"})
    brief = updated["prompt"].split("Token budget (winnow):")[1]

    assert len(brief) < 600
