"""Editor/agent integration.

The most reliable way to use Winnow is the explicit prefix form
``wn run -- <command>``. For hands-off use inside Claude Code, this module also
implements a PreToolUse hook that rewrites eligible ``Bash`` commands to the
wrapped form automatically. It only touches simple, read-heavy commands and
never rewrites anything containing shell metacharacters, so it can't change the
meaning of a pipeline.
"""

from __future__ import annotations

import json
import shlex
import sys
from typing import Optional

# First tokens we consider worth wrapping. Everything else is left alone.
_WRAP = {
    "git", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "docker", "kubectl",
    "pytest", "make", "cmake", "ninja", "ls", "dir", "tail", "journalctl",
    "curl", "wget", "go", "gradle", "mvn", "terraform",
}
# Shell metacharacters that mean "don't touch this — it's a pipeline".
_META = set("|&;<>`$(){}")


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


def _wrapped(command: str) -> Optional[str]:
    cmd = command.strip()
    if not cmd or any(ch in _META for ch in cmd):
        return None
    if cmd.startswith(("wn ", "winnow ")):
        return None
    try:
        first = shlex.split(cmd)[0]
    except ValueError:
        return None
    first = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if first not in _WRAP:
        return None
    return f"wn run -- {cmd}"


def run_hook(stdin_text: Optional[str] = None) -> int:
    """Entry point for ``wn hook run``. Reads a PreToolUse event on stdin and,
    if the command is eligible, emits a rewrite decision. Silence = no change.
    """
    data_raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        event = json.loads(data_raw) if data_raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    new_cmd = _wrapped(command)
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
