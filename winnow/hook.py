"""Editor/agent integration.

The most reliable way to use Winnow is the explicit prefix form
``wn run -- <command>``. For hands-off use inside Claude Code and Codex, this
module also implements a PreToolUse hook that rewrites eligible shell commands
to the wrapped form automatically. It only touches read-heavy commands and
keeps mutating commands fully visible.
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
_UNSAFE_CHAIN = re.compile(r"(?:;|&&|\|\||>|<|[\r\n])")
# Redirection that sends stdout to a file (``> out``, ``>> log``, ``1> out``).
# ``2>&1`` and friends are excluded: they merge streams we still want to read.
_FILE_REDIRECT = re.compile(r"(?:^|\s)\d?>>?\s*(?![&\d])\S")
_MUTATING = re.compile(
    r"(?:^|[\s;&|])(?:"
    r"remove-item|rm|del|erase|rmdir|rd|format-\w*|clear-content|"
    r"set-content|add-content|out-file|move-item|rename-item|copy-item|"
    r"stop-process|taskkill|shutdown|restart-computer|invoke-expression|iex"
    r")(?:\s|$)",
    re.IGNORECASE,
)


def settings_snippet(command: str = "wn hook run") -> dict:
    """Return a Claude Code settings.json fragment wiring up the hook."""
    return {
        "hooks": {
            "PreToolUse": [
                # Two matchers, not one. PowerShell is a separate tool with its
                # own name, so a Bash-only matcher never fires for it -- which
                # on a Windows workstation is most of the output there is.
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": command}],
                }
                for matcher in ("Bash", "PowerShell")
            ]
        }
    }


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
    if _MUTATING.search(cmd):
        return None
    shell_wrap = False
    if powershell:
        # Pipelines are safe here because the complete original script is
        # encoded and executed by PowerShell. Keep command chains and output
        # redirection unwrapped so their complete output remains visible.
        if _UNSAFE_CHAIN.search(cmd):
            return None
    elif any(ch in _META for ch in cmd):
        # A pipeline cannot be wrapped as bare argv, but handing the whole line
        # to ``sh -c`` lets Winnow read the combined output instead of skipping
        # the command outright. Redirection to a file stays unwrapped: those
        # bytes go to disk, so there is nothing for us to compress.
        if _FILE_REDIRECT.search(cmd):
            return None
        shell_wrap = True
    try:
        first = shlex.split(cmd)[0]
    except ValueError:
        return None
    first = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].casefold()
    if first not in _WRAP:
        return None
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
    if short_name not in {"Bash", "PowerShell", "shell_command", "exec_command"}:
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    is_codex = bool(event.get("turn_id") or os.environ.get("CODEX_THREAD_ID"))
    client = "codex" if is_codex else "claude"
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
