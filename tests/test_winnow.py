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


def test_a_redirect_target_that_starts_with_a_digit_is_still_a_redirect():
    """The digit that marks a stream sits before the operator, as in ``1>&2``.
    A digit after it belongs to the target and the target is an ordinary file.
    Reading those as stream merges sent the commands through the compressor
    with every byte already on its way to disk.
    """
    for cmd in ("cargo tree > 1.log", "cargo tree >2026.txt", "cargo tree 2> e.txt"):
        assert hook._wrapped(cmd) is None, cmd
        assert hook._wrapped(cmd, powershell=True) is None, cmd
    # The merges this guard exists for are untouched by the narrower rule.
    for cmd in ("cargo test 2>&1", "cargo test *>&1", "cargo test 1>&2"):
        assert hook._wrapped(cmd, powershell=True) is not None, cmd


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


def test_a_stream_merge_is_not_a_file_redirect_on_powershell():
    """``2>&1`` was read as redirection and killed the wrap. The bytes come
    back to the agent, so there is everything to compress. Bash already had
    this right, and both shells now go through the one rule.
    """
    merged = "cargo test 2>&1 | Select-String error"
    out = hook._wrapped(merged, powershell=True)
    assert out is not None
    assert base64.b64decode(out.rsplit(" ", 1)[-1]).decode("utf-16le") == merged
    for variant in ("cargo test 2>&1", "cargo test *>&1", "cargo test 2>>&1"):
        assert hook._wrapped(variant, powershell=True) is not None, variant
    # A redirect that lands in a file still bails: those bytes go to disk.
    assert hook._wrapped("cargo test 2>&1 > out.txt", powershell=True) is None


def test_a_leading_directory_change_no_longer_hides_the_real_command():
    """``shlex.split(cmd)[0]`` returned ``cd``, which is in no wrap set, so the
    dominant shape on Windows was skipped on both shells.
    """
    ps = "cd C:\\X; cargo test"
    out = hook._wrapped(ps, powershell=True)
    # The cd is not stripped. The whole line runs as one unit, so the directory
    # change still happens before the command that needs it.
    assert base64.b64decode(out.rsplit(" ", 1)[-1]).decode("utf-16le") == ps

    assert hook._wrapped("cd /c/X && cargo test") == (
        "wn run -- sh -c 'cd /c/X && cargo test'"
    )
    assert hook._wrapped("cd C:\\X; cargo test 2>&1 | Select-String e",
                         powershell=True) is not None
    for shape in ("cd X; cargo test", "cd X;cargo test",
                  "Set-Location X; cargo test", "pushd X; cargo test"):
        assert hook._wrapped(shape, powershell=True) is not None, shape


def test_the_guards_still_read_the_whole_line_not_just_the_segment():
    """Eligibility is decided from the segment after the cd. Everything that
    keeps a command visible is still decided from all of it.
    """
    assert hook._wrapped("cd X; rm -rf y") is None
    assert hook._wrapped("cd X; rm -rf y", powershell=True) is None
    assert hook._wrapped("cd X; Remove-Item y -Force", powershell=True) is None
    assert hook._wrapped("cd X; cargo tree > out.txt") is None
    assert hook._wrapped("cd X; cargo tree > out.txt", powershell=True) is None
    # A second chain past the cd is still a chain on PowerShell.
    assert hook._wrapped("cd X; cargo test; npm test", powershell=True) is None
    # And an ineligible command is still ineligible wherever it sits.
    assert hook._wrapped("cd X; ffprobe movie.mp4") is None


def test_a_line_that_cannot_be_split_confidently_is_left_alone():
    """A path holding a ``;`` must not desync the segment split, and an
    unbalanced quote is a line to leave alone rather than guess at.
    """
    quoted = 'cd "C:\\a;b" ; cargo test'
    assert hook._wrapped(quoted, powershell=True) is not None
    # The quoted ';' is part of the path, not a join, so nothing reads the
    # fragment after it as a command.
    assert hook._wrapped('cd "C:\\a;rm -rf y" ; cargo test') is None
    assert hook._wrapped('cargo test "unbalanced') is None
    assert hook._wrapped("cd X | cargo test", powershell=True) is None
    assert hook._wrapped("cd X", powershell=True) is None


def test_the_probe_shapes_survive_the_full_hook(isolated_home, capsys):
    """The six shapes measured against the live counters, end to end."""
    wrapped = {
        ("PowerShell", "cargo test"): True,
        ("PowerShell", "cd C:\\X; cargo test"): True,
        ("PowerShell", "cargo test 2>&1 | Select-String e"): True,
        ("PowerShell", "cd C:\\X; cargo test 2>&1 | Select-String e"): True,
        ("Bash", "cd /c/X && cargo test"): True,
        ("Bash", "cargo test | head -20"): True,
        ("Bash", "cd /c/X && rm -rf build"): False,
    }
    for (tool, command), expected in wrapped.items():
        event = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
        assert hook.run_hook(event) == 0
        out = capsys.readouterr().out
        assert bool(out.strip()) is expected, f"{tool}: {command}"


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


# --------------------------------------------------------------------------- #
# input clamping: Read and Grep
# --------------------------------------------------------------------------- #
def _decision(capsys, tool_name, tool_input):
    """Run the hook on one event and return its updatedInput, or None."""
    event = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    assert hook.run_hook(event) == 0
    out = capsys.readouterr().out.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]


def _big_file(tmp_path, name="big.txt"):
    """A file comfortably over the size threshold, without counting lines."""
    path = tmp_path / name
    path.write_text(("x" * 100 + "\n") * 2000, encoding="utf-8")
    assert path.stat().st_size > Config().read_large_file_bytes
    return path


def test_grep_without_a_head_limit_gets_one_per_output_mode(
    isolated_home, capsys
):
    """Content rows carry a whole source line each, path rows carry a path, so
    the two modes are not worth the same cap.
    """
    cfg = Config()
    content = _decision(capsys, "Grep", {
        "pattern": "TODO", "output_mode": "content"})
    assert content["head_limit"] == cfg.grep_head_limit_content

    for mode in ("files_with_matches", "count"):
        paths = _decision(capsys, "Grep", {"pattern": "TODO", "output_mode": mode})
        assert paths["head_limit"] == cfg.grep_head_limit_paths, mode

    # The tool's own default when output_mode is omitted is files_with_matches.
    default = _decision(capsys, "Grep", {"pattern": "TODO"})
    assert default["head_limit"] == cfg.grep_head_limit_paths
    assert cfg.grep_head_limit_content < cfg.grep_head_limit_paths


def test_an_explicit_head_limit_is_never_second_guessed(isolated_home, capsys):
    """Including 0, which the tool reads as unlimited. A caller who typed a
    number has already made this decision.
    """
    for limit in (5, 250, 4000, 0):
        assert _decision(capsys, "Grep", {
            "pattern": "TODO", "output_mode": "content", "head_limit": limit,
        }) is None, limit


def test_a_read_is_clamped_only_when_the_file_is_actually_large(
    isolated_home, tmp_path, capsys
):
    """Read already stops at 2000 lines, so clamping a short file would cost a
    complete read and save nothing.
    """
    big = _big_file(tmp_path)
    small = tmp_path / "small.txt"
    small.write_text("hello\n", encoding="utf-8")

    clamped = _decision(capsys, "Read", {"file_path": str(big)})
    assert clamped["limit"] == Config().read_clamp_lines
    assert _decision(capsys, "Read", {"file_path": str(small)}) is None


def test_a_read_with_its_own_limit_is_left_alone(
    isolated_home, tmp_path, capsys
):
    big = _big_file(tmp_path)
    assert _decision(capsys, "Read", {
        "file_path": str(big), "limit": 1500}) is None


def test_a_read_of_an_unreadable_path_emits_nothing_and_does_not_raise(
    isolated_home, tmp_path, capsys
):
    """A stat that fails must never turn into a failed tool call."""
    for target in (str(tmp_path / "nope.txt"), str(tmp_path), "", None):
        assert _decision(capsys, "Read", {"file_path": target}) is None, target
    assert _decision(capsys, "Read", {}) is None


def test_the_clamped_input_carries_every_original_parameter(
    isolated_home, tmp_path, capsys
):
    """updatedInput replaces the input rather than patching it, so anything
    left out is dropped. For Grep that means losing the pattern.
    """
    grep_input = {
        "pattern": r"def \w+", "path": "winnow", "output_mode": "content",
        "-n": True, "-C": 2, "glob": "*.py", "multiline": False,
    }
    updated = _decision(capsys, "Grep", dict(grep_input))
    assert set(updated) == set(grep_input) | {"head_limit"}
    assert all(updated[k] == v for k, v in grep_input.items())

    read_input = {"file_path": str(_big_file(tmp_path)), "offset": 900}
    updated = _decision(capsys, "Read", dict(read_input))
    assert set(updated) == set(read_input) | {"limit"}
    # offset says where to start reading, not how much to read.
    assert updated["offset"] == 900


def test_a_hand_edited_config_value_disables_the_clamp_instead_of_raising(
    isolated_home, tmp_path, capsys
):
    """Config.load assigns whatever config.json holds without checking its
    type. README says 0 turns a clamp off, so null is a reasonable guess for a
    reader who wants the same, and it used to reach int() and raise on every
    Read and Grep the agent made.
    """
    home = pathlib.Path(os.environ["WINNOW_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"grep_head_limit_content": None, "read_clamp_lines": "lots"}),
        encoding="utf-8",
    )
    assert _decision(capsys, "Grep", {
        "pattern": "TODO", "output_mode": "content"}) is None
    assert _decision(capsys, "Read", {"file_path": str(_big_file(tmp_path))}) is None
    # A key left alone still clamps: one bad value is not a broken config.
    assert _decision(capsys, "Grep", {"pattern": "TODO"})["head_limit"] == (
        Config().grep_head_limit_paths
    )


def test_glob_is_left_alone_because_there_is_nothing_to_clamp(
    isolated_home, capsys
):
    """Glob takes a pattern and a path and no limit of any kind, so the only
    way to shrink its result is to change what was asked for.
    """
    assert _decision(capsys, "Glob", {"pattern": "**/*.py"}) is None
    assert "Glob" not in hook._CLAMP_TOOLS


def test_clamps_are_counted_apart_from_compression(
    isolated_home, tmp_path, capsys, monkeypatch
):
    """A clamp caps a request; it never compresses an output. Counting it in
    the compression columns would publish a ratio Winnow never achieved.
    """
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    _decision(capsys, "Grep", {"pattern": "TODO"})
    _decision(capsys, "Grep", {"pattern": "TODO", "head_limit": 9})
    _decision(capsys, "Read", {"file_path": str(_big_file(tmp_path))})

    collector = efficiency.Collector()
    row = collector.snapshot()["claude"]
    collector.close()

    assert row.inputs_seen == 3
    assert row.inputs_clamped == 2
    assert row.clamp_pct == pytest.approx(66.67, abs=0.01)
    # None of it leaked into the shell or compression counters.
    assert (row.observed, row.selected, row.runs, row.raw_tokens) == (0, 0, 0, 0)
    assert row.reduction_pct == 0.0


def test_the_hook_snippet_covers_the_tools_it_can_act_on(isolated_home):
    matchers = [
        entry["matcher"]
        for entry in hook.settings_snippet()["hooks"]["PreToolUse"]
    ]
    assert matchers == ["Bash", "PowerShell", "Read", "Grep"]
    # Glob has no matcher because the hook would have nothing to do in it.
    assert "Glob" not in matchers


def test_install_writes_every_matcher_and_stays_idempotent(tmp_path, capsys):
    """It appended entry [0] and stopped, so the PowerShell matcher never
    reached settings.json through it, and Read and Grep would not have either.
    """
    settings = tmp_path / "settings.json"
    assert cli._hook_install(str(settings)) == 0
    assert cli._hook_install(str(settings)) == 0
    capsys.readouterr()

    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert [entry["matcher"] for entry in pre] == [
        "Bash", "PowerShell", "Read", "Grep"]


def test_gemini_runs_are_counted(tmp_path):
    """Before this label existed, `wn run --client gemini` recorded nothing:
    detect_client mapped it to "unknown" and both writers dropped every unknown
    label without a word. Gemini has no hook system, so explicit runs are the
    only numbers it can ever contribute.
    """
    collector = efficiency.Collector(tmp_path / "e.sqlite3")
    collector.record_run("gemini", raw_tokens=1000, output_tokens=250,
                         compressed=True, process_ns=1)
    # The editor's name reaches the same row as the model's.
    collector.observe("antigravity", selected=True)

    row = collector.snapshot()["gemini"]
    collector.close()

    assert row.runs == 1
    assert row.saved_tokens == 750
    assert row.observed == 1


def test_the_editor_name_is_an_alias_for_the_runtime():
    """Antigravity is where Gemini runs, not a runtime of its own. Counting it
    as a fourth label would have split one runtime's numbers across two rows.
    """
    for spelling in ("gemini", "antigravity", "gemini-antigravity", "GEMINI"):
        assert efficiency.detect_client(spelling) == "gemini", spelling


def test_a_runtime_can_name_itself_through_the_environment(monkeypatch):
    """No variable identifies Gemini or the editor it runs in, and inventing
    one would have given a label that never matched anything.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert efficiency.detect_client(env={"WINNOW_CLIENT": "gemini"}) == "gemini"
    # An explicit --client still wins over the environment.
    assert efficiency.detect_client("codex", env={"WINNOW_CLIENT": "gemini"}) == "codex"


# --------------------------------------------------------------------------- #
# search results
# --------------------------------------------------------------------------- #
def test_search_hits_are_capped_per_file_not_folded_by_similarity():
    """The two largest outputs winnow ever stored were ripgrep runs, 76 and 25
    million tokens, and no rule touched them. Hits differ in line number and in
    content, so collapse_repeats sees nothing repeated. What is wasteful is
    volume from a few files, which is what this caps.
    """
    body = [f"src/server.rs:{i * 3}:    call_{i}(ctx) and then other_{i}()"
            for i in range(1, 41)]
    body.append("docs/notes.md:12:  one mention in prose")
    out, removed = patterns.limit_per_file("\n".join(body), keep=5)

    assert removed == 35
    kept = [ln for ln in out.split("\n") if ln.startswith("src/server.rs:")]
    assert len(kept) == 5, "only the first five hits from the busy file survive"
    # The file that matched once is still there. Losing it would defeat the
    # point: a reader wants to know *which* files matched.
    assert "docs/notes.md:12:  one mention in prose" in out
    assert "+35 more in src/server.rs" in out


def test_a_file_under_the_cap_is_untouched():
    text = "a.rs:1:one\na.rs:2:two\nb.rs:9:three"
    out, removed = patterns.limit_per_file(text, keep=5)
    assert removed == 0
    assert out == text


def test_lines_that_are_not_search_hits_always_survive():
    """A rule that eats the one line explaining an empty result is worse than
    no rule at all.
    """
    text = "\n".join(
        [f"src/x.rs:{i}:hit {i}" for i in range(1, 30)]
        + ["Binary file logo.png matches",
           "rg: ./locked: Permission denied (os error 13)",
           "no matches found"]
    )
    out, _ = patterns.limit_per_file(text, keep=3)

    assert "Binary file logo.png matches" in out
    assert "rg: ./locked: Permission denied (os error 13)" in out
    assert "no matches found" in out


def test_the_ripgrep_rule_is_wired_to_the_action():
    """A rule naming an action the engine does not implement is a rule that
    silently does nothing, which is how the file-list rule would have shipped.
    """
    body = [f"src/server.rs:{i}:    unique_call_{i}(a, b) plus tail_{i}"
            for i in range(1, 40)]
    out, applied = rules.apply_rules(
        "rg -n unique_call", "\n".join(body), rules.load_rules(), Config())

    assert "ripgrep" in applied
    assert "more in src/server.rs" in out
    assert len(out.split("\n")) < 15
