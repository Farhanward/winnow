"""Test suite for Winnow.

Runs fully offline. Each test that touches the store points WINNOW_HOME at a
temporary directory so nothing leaks into the user's real recall store.
"""

import base64
import json
import os
import pathlib
import re
from argparse import Namespace
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from winnow import cli, core, efficiency, hook, patterns, rules, semantic, tokens
from winnow.config import Config
from winnow.filters import detect
from winnow.store import Store, is_handle


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNOW_HOME", str(tmp_path / "wh"))
    return tmp_path


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
def test_token_estimate_monotonic():
    assert tokens.estimate("") == 0
    assert tokens.estimate("a") >= 1
    assert tokens.estimate("word " * 100) > tokens.estimate("word " * 10)


# --------------------------------------------------------------------------- #
# built-in filters
# --------------------------------------------------------------------------- #
def test_npm_filter_drops_warnings():
    raw = "\n".join(["npm warn deprecated x"] * 30 + ["added 5 packages"])
    func, name = detect("npm install")
    assert name == "npm-install"
    out = func(raw, Config())
    assert "added 5 packages" in out
    # No actual warning lines survive (the summary line may mention the words).
    assert not any(ln.startswith("npm warn") for ln in out.split("\n"))
    assert "hidden" in out


def test_pip_filter_keeps_outcome():
    raw = "\n".join(
        ["Requirement already satisfied: numpy in ./v"] * 10
        + ["Successfully installed numpy-2.0.0"]
    )
    func, _ = detect("pip install numpy")
    out = func(raw, Config())
    assert "Successfully installed numpy-2.0.0" in out
    assert "Requirement already satisfied" not in out


def test_git_status_drops_hints():
    raw = (
        'On branch main\n'
        'Changes to be committed:\n'
        '  (use "git restore --staged <file>..." to unstage)\n'
        '        modified:   a.py\n'
    )
    func, _ = detect("git status")
    out = func(raw, Config())
    assert "(use" not in out
    assert "modified:   a.py" in out


# --------------------------------------------------------------------------- #
# rules engine
# --------------------------------------------------------------------------- #
def test_rule_drop_and_summary():
    rule = [{
        "name": "t", "match": "foo",
        "actions": [{"drop_lines": "^noise"}, {"summary": "hid {dropped}"}],
    }]
    text = "keep\nnoise 1\nnoise 2\nkeep2"
    out, applied = rules.apply_rules("foo", text, rule, Config())
    assert applied == ["t"]
    assert "noise" not in out
    assert "hid 2" in out


def test_rule_no_match_is_noop():
    rule = [{"name": "t", "match": "bar", "actions": [{"drop_lines": ".*"}]}]
    out, applied = rules.apply_rules("foo", "hello", rule, Config())
    assert out == "hello" and applied == []


def test_bad_regex_does_not_crash():
    rule = [{"name": "t", "match": "foo", "actions": [{"drop_lines": "([unclosed"}]}]
    out, _ = rules.apply_rules("foo", "hello", rule, Config())
    assert out == "hello"


# --------------------------------------------------------------------------- #
# patterns
# --------------------------------------------------------------------------- #
def test_collapse_repeats():
    text = "\n".join(f"connecting to host id={i}" for i in range(50))
    out, removed = patterns.collapse_repeats(text, threshold=3)
    assert removed > 40
    assert "×50" in out or "×" in out


def test_fingerprint_normalises_numbers_and_uuids():
    a = patterns.fingerprint("req 12345 done in 43ms")
    b = patterns.fingerprint("req 99 done in 7ms")
    assert a == b


def test_cascade_guard():
    text = "\n".join(["ok"] + [f"ERROR timeout {i}" for i in range(30)])
    out, removed = patterns.cascade_guard(text, max_occurrences=5)
    assert removed > 20
    assert "more like this" in out


# --------------------------------------------------------------------------- #
# semantic
# --------------------------------------------------------------------------- #
def test_json_array_truncation():
    data = {"items": list(range(500))}
    out, changed = semantic.compress_json(json.dumps(data))
    assert changed
    assert "more of 500 items" in out
    assert len(out) < len(json.dumps(data))


def test_json_rejects_non_json():
    assert semantic.compress_json("just some text") is None


def test_skim_python():
    src = (
        "import os\n"
        "class Foo:\n"
        '    """A class."""\n'
        "    def bar(self, x):\n"
        '        """Do bar."""\n'
        "        return x + 1\n"
    )
    out = semantic.skim_python(src)
    assert "class Foo" in out
    assert "def bar(self, x):" in out
    assert "return x + 1" not in out  # body elided


# --------------------------------------------------------------------------- #
# store + recall
# --------------------------------------------------------------------------- #
def test_store_put_get_search(isolated_home):
    s = Store()
    h = s.put("git log", "/repo", 0, "hello world alpha", 5, 2, "test")
    assert is_handle(h)
    rec = s.get(h)
    assert rec is not None and rec.raw == "hello world alpha"
    hits = s.search("alpha")
    assert any(r.id == h for r in hits)
    s.close()


# --------------------------------------------------------------------------- #
# core pipeline
# --------------------------------------------------------------------------- #
def test_core_passthrough_small(isolated_home):
    r = core.compress("echo hi", "hi", remember=False)
    assert r.passthrough is True


def test_core_compresses_and_stores(isolated_home):
    s = Store()
    raw = "\n".join(["npm warn x"] * 100 + ["added 3 packages"])
    r = core.compress("npm install", raw, store=s, remember=True)
    assert r.passthrough is False
    assert r.comp_tokens < r.raw_tokens
    assert r.handle is not None
    assert s.get(r.handle).raw == raw  # full output recoverable
    assert "wn recall" in r.footer()
    s.close()


def test_core_json_path(isolated_home):
    raw = json.dumps({"items": list(range(1000))})
    r = core.compress("curl api", raw, remember=False)
    assert r.label == "json"
    assert r.comp_tokens < r.raw_tokens


def test_core_handles_powershell_utf8_bom(isolated_home):
    raw = "\ufeff" + "\n".join(
        ["npm warn deprecated sample"] * 80 + ["added 3 packages"]
    )
    r = core.compress("npm install", raw, remember=False)

    assert r.passthrough is False
    assert not any(line.startswith("\ufeffnpm warn") for line in r.body.splitlines())
    assert "80 npm warn/notice lines hidden" in r.body


def test_gain_history_uses_stored_filter_name(isolated_home, capsys):
    s = Store()
    raw = "\n".join(["npm warn x"] * 100 + ["added 3 packages"])
    core.compress("npm install", raw, store=s, remember=True)
    s.close()

    assert cli.cmd_gain(Namespace(history=True, limit=5)) == 0
    out = capsys.readouterr().out
    assert "recent:" in out
    assert "npm" in out


# --------------------------------------------------------------------------- #
# hook eligibility
# --------------------------------------------------------------------------- #
def test_hook_wraps_simple_command():
    out = hook._wrapped("git status")
    assert out == "wn run -- git status"


def test_hook_wraps_codex_powershell_search_without_changing_script():
    command = "rg -n TODO C:\\Projects | Select-Object -First 20"
    out = hook._wrapped(command, powershell=True)

    assert out.startswith(
        "wn run -- powershell -NoProfile -NonInteractive -EncodedCommand "
    )
    encoded = out.rsplit(" ", 1)[-1]
    assert base64.b64decode(encoded).decode("utf-16le") == command


def test_hook_wraps_codex_powershell_file_read():
    assert hook._wrapped(
        "Get-Content -LiteralPath C:\\large.log", powershell=True
    )


def test_hook_wraps_pipelines_through_sh():
    # A pipeline cannot be wrapped as bare argv, so the whole line goes to
    # `sh -c` and Winnow reads the combined output.
    assert hook._wrapped("git log | head") == "wn run -- sh -c 'git log | head'"
    assert hook._wrapped("cargo check 2>&1 | tail -25") == (
        "wn run -- sh -c 'cargo check 2>&1 | tail -25'"
    )


def test_hook_skips_redirection_to_file():
    # The bytes land on disk, so there is nothing left for Winnow to compress.
    assert hook._wrapped("cargo tree > out.txt") is None
    assert hook._wrapped("cargo tree >> log") is None
    assert hook._wrapped("cat x > y") is None


def test_hook_still_skips_unwrappable_first_tokens_in_pipelines():
    assert hook._wrapped("ffprobe movie.mp4 | head -5") is None


def test_hook_skips_unknown_and_already_wrapped():
    assert hook._wrapped("rm -rf /tmp/x") is None
    assert hook._wrapped("wn run -- git status") is None


def test_powershell_path_is_unaffected_by_sh_wrapping():
    # Codex runs through the PowerShell branch. It must keep encoding the whole
    # script and must never be rewritten to `sh -c`.
    out = hook._wrapped("rg -n TODO . | Select-Object -First 5", powershell=True)
    assert "sh -c" not in out
    assert "-EncodedCommand" in out
    # Chains stay unwrapped on the PowerShell path, as before.
    assert hook._wrapped("rg -n TODO . > out.txt", powershell=True) is None


def test_hook_keeps_mutating_powershell_commands_unwrapped():
    assert hook._wrapped(
        "Get-Content input.txt | Remove-Item output.txt", powershell=True
    ) is None


def test_codex_hook_rewrites_shell_command(isolated_home, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "test-thread")
    event = json.dumps({
        "tool_name": "shell_command",
        "tool_input": {"command": "rg -n TODO C:\\Projects"},
        "turn_id": "test-turn",
    })

    assert hook.run_hook(event) == 0
    decision = json.loads(capsys.readouterr().out)
    updated = decision["hookSpecificOutput"]["updatedInput"]["command"]
    if os.name == "nt":
        assert updated.startswith("wn run --client codex -- powershell ")
    else:
        assert updated == "wn run --client codex -- rg -n TODO C:\\Projects"


def test_powershell_tool_is_wrapped_and_encoded(isolated_home, capsys):
    """The PowerShell tool reaches the PowerShell path.

    The encoding for it was written from the start and nothing could reach it:
    run_hook accepted three tool names, none of them this one, so every
    PowerShell call went through uncompressed. On a Windows workstation that is
    most of the output there is.
    """
    event = json.dumps({
        "tool_name": "PowerShell",
        "tool_input": {"command": "Get-VM"},
    })

    assert hook.run_hook(event) == 0
    updated = json.loads(capsys.readouterr().out)[
        "hookSpecificOutput"]["updatedInput"]["command"]
    assert updated.startswith("wn run --client claude -- powershell ")
    assert "-EncodedCommand" in updated
    # UTF-16LE base64, which is what -EncodedCommand takes and what a plain
    # utf-8 encode would silently get wrong.
    encoded = updated.rsplit(" ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-16le") == "Get-VM"


def test_powershell_mutating_command_is_left_alone(isolated_home, capsys):
    """A command that changes something stays fully visible, as on Bash."""
    event = json.dumps({
        "tool_name": "PowerShell",
        "tool_input": {"command": "Remove-Item /tmp/x -Force"},
    })

    assert hook.run_hook(event) == 0
    assert capsys.readouterr().out.strip() == ""


def test_the_new_readers_are_wrapped(isolated_home, capsys):
    """ssh, pkg and python: the three that carried this session's output."""
    for command in ("ssh root@host uname -a", "pkg install -y redis",
                    "python emit.py --check"):
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        assert hook.run_hook(event) == 0
        out = capsys.readouterr().out
        assert "wn run" in out, f"{command!r} was left unwrapped"


def test_every_cmdlet_named_in_a_rule_can_reach_the_wrapper():
    """A rule that names a cmdlet the hook never wraps is a rule that never
    runs. The powershell-tables rule listed Get-Volume and Get-Partition while
    _WRAP admitted neither, so both were written and both were dead.
    """
    rules = (pathlib.Path(hook.__file__).parent
             / "rules_data" / "40-remote.yaml").read_text(encoding="utf-8")
    alternation = re.search(r"Get-\(([^)]+)\)", rules)
    assert alternation, "the powershell-tables rule no longer looks as expected"

    for suffix in alternation.group(1).split("|"):
        assert f"get-{suffix.casefold()}" in hook._WRAP, (
            f"the rule names Get-{suffix} but the hook will never wrap it")


def test_antigravity_runs_are_counted(tmp_path):
    """Before this label existed, `wn run --client antigravity` recorded
    nothing: detect_client mapped it to "unknown" and both writers dropped
    every unknown label without a word. Antigravity has no hook system, so
    explicit runs are the only numbers it can ever contribute.
    """
    collector = efficiency.Collector(tmp_path / "e.sqlite3")
    collector.record_run("antigravity", raw_tokens=1000, output_tokens=250,
                         compressed=True, process_ns=1)
    collector.observe("gemini", selected=True)

    row = collector.snapshot()["antigravity"]
    collector.close()

    assert row.runs == 1
    assert row.saved_tokens == 750
    assert row.observed == 1


def test_a_runtime_can_name_itself_through_the_environment(monkeypatch):
    """No variable identifies Antigravity, and inventing one would have given
    a label that never matched anything.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert efficiency.detect_client(env={"WINNOW_CLIENT": "antigravity"}) == "antigravity"
    # An explicit --client still wins over the environment.
    assert efficiency.detect_client("codex", env={"WINNOW_CLIENT": "antigravity"}) == "codex"
