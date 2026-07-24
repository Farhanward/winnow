"""Test suite for Winnow.

Runs fully offline. Each test that touches the store points WINNOW_HOME at a
temporary directory so nothing leaks into the user's real recall store.
"""

import base64
import json
import os
from argparse import Namespace
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from winnow import cli, core, hook, patterns, rules, semantic, tokens
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


def test_hook_skips_pipelines():
    assert hook._wrapped("git log | head") is None
    assert hook._wrapped("cat x > y") is None


def test_hook_skips_unknown_and_already_wrapped():
    assert hook._wrapped("rm -rf /tmp/x") is None
    assert hook._wrapped("wn run -- git status") is None


def test_hook_keeps_mutating_powershell_commands_unwrapped():
    assert hook._wrapped(
        "Get-Content input.txt | Remove-Item output.txt", powershell=True
    ) is None


def test_codex_hook_rewrites_shell_command(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_THREAD_ID", "test-thread")
    event = json.dumps({
        "tool_name": "shell_command",
        "tool_input": {"command": "rg -n TODO C:\\Projects"},
        "turn_id": "test-turn",
    })

    assert hook.run_hook(event) == 0
    decision = json.loads(capsys.readouterr().out)
    updated = decision["hookSpecificOutput"]["updatedInput"]["command"]
    assert updated.startswith("wn run -- powershell ")
