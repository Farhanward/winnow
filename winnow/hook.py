"""Editor/agent integration.

The most reliable way to use Winnow is the explicit prefix form
``wn run -- <command>``. For hands-off use inside Claude Code and Codex, this
module also implements a PreToolUse hook that rewrites eligible shell commands
to the wrapped form automatically. It only touches read-heavy commands and
keeps mutating commands fully visible.

The same hook clamps the *input* of Claude Code's ``Read`` and ``Grep``. Those
tools build their output inside the agent, so there is no output for Winnow to
compress; capping the request is the only lever left, and it caps the cost at
the source.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys
from typing import Optional

# First tokens we consider worth wrapping. Everything else is left alone.
_WRAP = {
    "git", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "docker", "kubectl",
    "pytest", "make", "cmake", "ninja", "ls", "dir", "tail", "journalctl",
    "curl", "wget", "go", "gradle", "mvn", "terraform", "rg", "ripgrep",
    "get-content", "gc", "type", "select-string", "sls", "get-childitem",
    "gci", "get-process", "gps", "get-service", "get-winevent",
    "get-ciminstance", "get-command", "get-module", "tree",
    # Added after a session where the biggest outputs came from none of the
    # above: a remote shell, a package manager on the far end of it, and the
    # scripts driving both.
    "ssh", "scp", "sftp", "pkg", "pkgin", "apt", "apt-get", "dnf", "yum",
    "brew", "python", "python3", "py", "get-vm", "get-vhd", "get-disk",
    "get-netipaddress", "get-vmswitch", "test-netconnection", "get-item",
    # Kept in step with the powershell-tables rule in 40-remote.yaml: a rule
    # that names a cmdlet this set does not admit is a rule that never runs.
    "get-volume", "get-partition",
}
# Shell metacharacters that make direct argv wrapping ambiguous.
_META = set("|&;<>`$(){}")
# Redirection that sends stdout to a file (``> out``, ``>> log``, ``1> out``,
# ``*> all``). Stream merges are excluded: ``2>&1``, ``*>&1`` and ``2>>&1``
# hand the bytes back to us, so there is everything to compress. The operator
# is matched as a whole run of ``>`` so that ``2>>&1`` cannot be read as
# ``2>`` followed by a file named ``>&1``.
# Both shells are held to this one rule. PowerShell used to bail on any ``>``
# at all, which caught ``2>&1`` along with it and left the most common way an
# agent asks for stderr uncompressed.
# The trailing guard excludes ``&`` only. A digit after the operator belongs to
# the target, as in ``> 1.log``; the digit that marks a stream sits *before* it,
# as in ``1>&2``, and ``[\d*]?`` with the guard above already accounts for that
# one. Excluding digits here read every target that opened with one as a merge
# and sent those commands through the compressor with nothing on stdout.
_FILE_REDIRECT = re.compile(r"(?:^|\s)[\d*]?>{1,2}(?![>&])\s*(?!&)\S")
_NEWLINE = re.compile(r"[\r\n]")
# Tokens that join two commands into one line. Only ``;`` and ``&&`` continue
# past a leading directory change; the rest stay a reason to leave the line
# alone on PowerShell, where the whole script is handed over as one unit.
_CHAIN = {";", "&&", "||", "&", "<", "<<"}
_CONTINUES = {";", "&&"}
# Openers that say nothing about what a line will print. ``cd X; cargo test``
# is the dominant shape on Windows, and judging it by its first token judged
# every one of them as ``cd``.
_DIR_CHANGE = {"cd", "chdir", "sl", "set-location", "pushd", "push-location"}
_MUTATING = re.compile(
    r"(?:^|[\s;&|])(?:"
    r"remove-item|rm|del|erase|rmdir|rd|format-\w*|clear-content|"
    r"set-content|add-content|out-file|move-item|rename-item|copy-item|"
    r"stop-process|taskkill|shutdown|restart-computer|invoke-expression|iex"
    r")(?:\s|$)",
    re.IGNORECASE,
)


# Tools whose command line the hook rewrites into the wrapped form.
_SHELL_TOOLS = {"Bash", "PowerShell", "shell_command", "exec_command"}
# Tools whose parameters the hook clamps. Nothing here is compressed: the
# output is assembled inside the agent and the hook never sees a byte of it.
#
# Glob is deliberately absent. Its schema is ``pattern`` and ``path`` and
# nothing else: no limit, no head, no count. The only way to make it return
# less is to rewrite the pattern, which changes what the caller asked for.
# There is nothing here to clamp, so please do not add it back.
_CLAMP_TOOLS = {"Read", "Grep"}


def settings_snippet(command: str = "wn hook run") -> dict:
    """Return a Claude Code settings.json fragment wiring up the hook."""
    return {
        "hooks": {
            "PreToolUse": [
                # One matcher per tool, not one for all of them. PowerShell is
                # a separate tool with its own name, so a Bash-only matcher
                # never fires for it -- which on a Windows workstation is most
                # of the output there is. Read and Grep are here for the other
                # half of the job: their output cannot be compressed, so the
                # hook caps their input instead.
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": command}],
                }
                for matcher in ("Bash", "PowerShell", "Read", "Grep")
            ]
        }
    }


def _clamped_grep(tool_input: dict, cfg):
    """Return (input with a head_limit, note), or None to leave the call alone.

    The note goes back to the model. A cap it cannot see is worse than no cap:
    it would read a truncated result as the whole result and conclude that
    nothing else matches.
    """
    if tool_input.get("head_limit") is not None:
        # An explicit head_limit is a decision the caller already made, and
        # that includes an explicit 0 meaning unlimited. Do not second-guess it.
        return None
    mode = str(tool_input.get("output_mode") or "files_with_matches")
    limit = (
        cfg.grep_head_limit_content if mode == "content"
        else cfg.grep_head_limit_paths
    )
    limit = int(limit)
    if limit <= 0:
        return None
    updated = dict(tool_input)
    updated["head_limit"] = limit
    note = (
        f"winnow set head_limit={limit} on this Grep because the call carried "
        "none. Results past that row are not shown and there may be more "
        "matches. Pass an explicit head_limit to override, 0 for unlimited."
    )
    return updated, note


def _clamped_read(tool_input: dict, cfg):
    """Return (input with a line limit, note), or None to leave the call alone.

    The note goes back to the model, because the failure mode of a silent cap
    here is a wrong conclusion rather than a missing line: a file read to line
    400 of 12,000 looks like a complete file that simply ends there.
    """
    if tool_input.get("limit") is not None:
        return None
    lines = int(cfg.read_clamp_lines)
    threshold = int(cfg.read_large_file_bytes)
    if lines <= 0 or threshold <= 0:
        return None
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    try:
        # A stat, not a line count. This runs on every Read the agent makes,
        # so opening the file to count lines would put real I/O on the hot
        # path to answer a question a size already answers well enough. A
        # missing or unreadable path clamps nothing and raises nothing.
        size = os.path.getsize(path)
    except (OSError, ValueError):
        return None
    if size < threshold:
        # Under the threshold the tool's own 2000-line cap is doing the work
        # already, so clamping here would only truncate a cheap read.
        return None
    updated = dict(tool_input)
    updated["limit"] = lines
    # offset is left alone: it says where the caller wanted to start reading,
    # and moving it would return different content, not less of it.
    start = int(tool_input.get("offset") or 1)
    note = (
        f"winnow capped this Read to {lines} lines because the file is "
        f"{size:,} bytes. You are seeing lines {start}-{start + lines - 1}, "
        "not the whole file. Read again with an explicit offset to continue, "
        "or an explicit limit to override the cap."
    )
    return updated, note


def _clamp_input(short_name: str, tool_input: dict, client: str) -> int:
    """Cap what Read/Grep ask for and emit the full input back."""
    from . import config as _config
    from . import efficiency

    try:
        cfg = _config.Config.load()
    except OSError:
        cfg = _config.Config()
    try:
        if short_name == "Grep":
            clamp = _clamped_grep(tool_input, cfg)
        else:
            clamp = _clamped_read(tool_input, cfg)
    except (TypeError, ValueError):
        # config.json is edited by hand and Config.load assigns whatever it
        # finds without checking the type, so a null or a stray string reaches
        # the int() calls above. README says 0 turns a clamp off, which makes
        # null a reasonable guess for a reader after the same thing. A setting
        # we cannot read turns its clamp off; it must never fail the tool call.
        clamp = None
    efficiency.observe_input(client, clamped=clamp is not None)
    if clamp is None:
        return 0
    updated, note = clamp
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            # The model is told what was capped and how to get the rest. A cap
            # it cannot see is the one way this feature could do harm: it would
            # read a truncated result as a complete one and reason from it.
            "additionalContext": note,
            # The complete tool_input, not only the key that changed. The
            # shell path can send {"command": ...} alone because command is
            # the whole of what Bash and PowerShell take; Read and Grep carry
            # several parameters, and a partial updatedInput would drop
            # file_path or pattern and break the call outright.
            "updatedInput": updated,
        }
    }
    sys.stdout.write(json.dumps(decision))
    return 0


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].casefold()


def _tokens(command: str) -> Optional[list]:
    """Split a command line, keeping ``;`` and ``&&`` as tokens of their own.

    Quoting is respected, so a path holding a ``;`` stays one token instead of
    desyncing the split. Backslash escaping is off, because on Windows every
    second path contains one and none of them are escapes, and ``#`` is not a
    comment here for the same reason ``shlex.split`` does not treat it as one.
    Returns None when the line cannot be read, and the caller then leaves the
    command alone rather than guessing at it.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _eligibility(command: str) -> Optional[tuple]:
    """Return ``(deciding token, chained)`` for a line, or None if unreadable.

    The deciding token is normally the first one. When the line opens with a
    directory change joined by ``;`` or ``&&``, it is the first token of the
    next segment instead, which is the command whose output actually arrives.
    The directory change is never stripped: the whole original line is what
    gets wrapped and run, so the ``cd`` still happens first.
    """
    tokens = _tokens(command)
    if not tokens:
        return None
    start = 0
    if _basename(tokens[0]) in _DIR_CHANGE:
        for i in range(1, len(tokens)):
            if tokens[i] in _CONTINUES:
                start = i + 1
                break
            if tokens[i] in _CHAIN:
                # Some other join right after the directory change. Reading
                # further would be guessing, so the line stays unwrapped.
                return None
        if not start:
            return None
    rest = tokens[start:]
    if not rest:
        return None
    return _basename(rest[0]), any(tok in _CHAIN for tok in rest)


def _wrapped(
    command: str,
    *,
    powershell: bool = False,
    client: Optional[str] = None,
) -> Optional[str]:
    cmd = command.strip()
    if not cmd:
        return None
    if cmd.startswith(("wn ", "winnow ")):
        return None
    # The mutating guard and the file-redirect guard below both read the whole
    # line, not the segment eligibility was decided from. ``cd X; rm -rf y``
    # has to stay visible even though its deciding token is a reader.
    if _MUTATING.search(cmd):
        return None
    eligible = _eligibility(cmd)
    if eligible is None:
        return None
    first, chained = eligible
    if first not in _WRAP:
        return None
    shell_wrap = False
    if powershell:
        # Pipelines are safe here because the complete original script is
        # encoded and executed by PowerShell. Redirection to a file and any
        # command chain past a leading directory change stay unwrapped so
        # their complete output remains visible.
        if _FILE_REDIRECT.search(cmd) or chained or _NEWLINE.search(cmd):
            return None
    elif any(ch in _META for ch in cmd):
        # A pipeline cannot be wrapped as bare argv, but handing the whole line
        # to ``sh -c`` lets Winnow read the combined output instead of skipping
        # the command outright. Redirection to a file stays unwrapped: those
        # bytes go to disk, so there is nothing for us to compress.
        if _FILE_REDIRECT.search(cmd):
            return None
        shell_wrap = True
    client_arg = f"--client {client} " if client else ""
    if powershell:
        encoded = base64.b64encode(cmd.encode("utf-16le")).decode("ascii")
        return (
            f"wn run {client_arg}-- powershell -NoProfile -NonInteractive "
            f"-EncodedCommand {encoded}"
        )
    if shell_wrap:
        return f"wn run {client_arg}-- sh -c {shlex.quote(cmd)}"
    return f"wn run {client_arg}-- {cmd}"


def run_hook(stdin_text: Optional[str] = None) -> int:
    """Entry point for ``wn hook run``. Reads a PreToolUse event on stdin and,
    if the command is eligible, emits a rewrite decision. Silence = no change.
    """
    data_raw = stdin_text if stdin_text is not None else sys.stdin.read()
    data_raw = data_raw.lstrip("﻿")  # tolerate a UTF-8 BOM on stdin
    try:
        event = json.loads(data_raw) if data_raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    tool_name = str(event.get("tool_name") or "")
    short_name = tool_name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    is_codex = bool(event.get("turn_id") or os.environ.get("CODEX_THREAD_ID"))
    client = "codex" if is_codex else "claude"
    if short_name in _CLAMP_TOOLS:
        return _clamp_input(short_name, event.get("tool_input") or {}, client)
    if short_name not in _SHELL_TOOLS:
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    # PowerShell needs its own quoting: the command is handed back base64 in
    # UTF-16LE rather than as bare argv, which is why _wrapped has taken this
    # flag since it was written.
    is_powershell = short_name == "PowerShell"
    new_cmd = _wrapped(
        command,
        # Two ways to arrive at PowerShell: the tool that is one, and Codex on
        # Windows, whose shell tool runs through it. The first was missing --
        # the flag existed and only Codex could ever set it.
        powershell=is_powershell or (os.name == "nt" and is_codex),
        client=client,
    )
    from . import efficiency

    already_wrapped = command.strip().startswith(("wn ", "winnow "))
    efficiency.observe(client, selected=bool(new_cmd) or already_wrapped)
    if not new_cmd:
        return 0
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": new_cmd},
        }
    }
    sys.stdout.write(json.dumps(decision))
    return 0


if __name__ == "__main__":
    # This module intentionally imports only the standard library so it adds
    # minimal latency to every shell command the PreToolUse hook inspects.
    raise SystemExit(run_hook())
