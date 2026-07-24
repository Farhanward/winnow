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
}
# Shell metacharacters that make direct argv wrapping ambiguous.
_META = set("|&;<>`$(){}")
_UNSAFE_CHAIN = re.compile(r"(?:;|&&|\|\||>|<|[\r\n])")
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
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _wrapped(command: str, *, powershell: bool = False) -> Optional[str]:
    cmd = command.strip()
    if not cmd:
        return None
    if cmd.startswith(("wn ", "winnow ")):
        return None
    if _MUTATING.search(cmd):
        return None
    if powershell:
        # Pipelines are safe here because the complete original script is
        # encoded and executed by PowerShell. Keep command chains and output
        # redirection unwrapped so their complete output remains visible.
        if _UNSAFE_CHAIN.search(cmd):
            return None
    elif any(ch in _META for ch in cmd):
        return None
    try:
        first = shlex.split(cmd)[0]
    except ValueError:
        return None
    first = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].casefold()
    if first not in _WRAP:
        return None
    if powershell:
        encoded = base64.b64encode(cmd.encode("utf-16le")).decode("ascii")
        return (
            "wn run -- powershell -NoProfile -NonInteractive "
            f"-EncodedCommand {encoded}"
        )
    return f"wn run -- {cmd}"


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
    if event.get("tool_name") not in {"Bash", "shell_command", "exec_command"}:
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    is_codex = bool(event.get("turn_id") or os.environ.get("CODEX_THREAD_ID"))
    new_cmd = _wrapped(command, powershell=os.name == "nt" and is_codex)
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
