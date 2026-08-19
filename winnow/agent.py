"""Agent-side accounting - the other half of the token bill.

Winnow began by compressing terminal output, which is the part of an agent's
context a hook can reach and rewrite. Measuring the result honestly showed how
small that part is. On the machine this was developed on, every output Winnow
ever compressed for Claude Code came to 236,177 tokens read. The same machine's
transcripts record 4.59 billion tokens of context re-read across 14,893 billed
requests. Terminal output was never where the money went.

This module reads what the agent already writes to disk and says where the
money did go. Nothing here is sent anywhere, and nothing here is a price
estimate: the numbers are the token counts the API returned, copied out of the
agent's own transcripts.

Two shapes of waste show up in that data and neither one is an output:

1. The floor. Every request re-reads the whole prompt prefix: system prompt,
   memory files, skill descriptions, and the tool schema of every connected
   MCP server. A session that starts at 55,000 tokens pays 55,000 tokens on
   its first request and on its four hundredth. Multiply by the request count
   and the fixed cost dwarfs anything a filter can trim.
2. Subagents. Each one starts cold, re-derives context the parent already had,
   and bills its own requests against the same budget.

Both are governed by configuration rather than by compression, which is why
this module reports and recommends instead of rewriting anything.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# The counters the API returns on every assistant message. cache_read is the
# one that matters here: it is the prompt prefix being read again, and it is
# the only column that grows with the length of a session rather than with the
# work done in it.
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def transcript_roots() -> List[Path]:
    """Return every local transcript directory that exists.

    Claude Code writes one JSONL file per session under ~/.claude/projects.
    Codex writes its own sessions under ~/.codex/sessions. Both are local
    files the agent already maintains; Winnow only reads them.
    """
    candidates = [
        Path.home() / ".claude" / "projects",
        Path.home() / ".codex" / "sessions",
    ]
    override = os.environ.get("WINNOW_TRANSCRIPTS")
    if override:
        candidates = [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    return [p for p in candidates if p.is_dir()]


def transcript_files(roots: Optional[Iterable[Path]] = None) -> List[Path]:
    paths: List[Path] = []
    for root in roots if roots is not None else transcript_roots():
        paths.extend(sorted(Path(root).rglob("*.jsonl")))
    return paths


@dataclass
class Session:
    id: str
    date: str = ""
    requests: int = 0
    floor: int = 0  # smallest whole-prompt read seen: the fixed cost per turn
    peak: int = 0
    tokens: Dict[str, int] = field(default_factory=dict)


@dataclass
class Report:
    files: int = 0
    sessions: List[Session] = field(default_factory=list)
    requests: int = 0
    subagent_requests: int = 0
    totals: Dict[str, int] = field(default_factory=dict)
    subagent_totals: Dict[str, int] = field(default_factory=dict)
    tool_calls: Dict[str, int] = field(default_factory=dict)
    tool_result_bytes: Dict[str, int] = field(default_factory=dict)
    mcp_calls: Dict[str, int] = field(default_factory=dict)
    skipped: int = 0

    @property
    def floor(self) -> int:
        """Median per-session floor.

        The median rather than the mean: one session opened in a directory
        with a large memory file drags a mean well past anything typical.
        """
        floors = [s.floor for s in self.sessions if s.floor]
        return int(statistics.median(floors)) if floors else 0

    @property
    def fixed_cost(self) -> int:
        """Tokens spent re-reading the floor, across every request counted."""
        return self.floor * self.requests

    @property
    def context_read(self) -> int:
        return self.totals.get("cache_read_input_tokens", 0)

    @property
    def subagent_share(self) -> float:
        if not self.context_read:
            return 0.0
        read = self.subagent_totals.get("cache_read_input_tokens", 0)
        return read * 100.0 / self.context_read


def _add(bucket: Dict[str, int], key: str, n: int) -> None:
    bucket[key] = bucket.get(key, 0) + n


def _usage(entry: dict) -> Dict[str, int]:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return {}
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return {}
    out = {k: int(usage.get(k) or 0) for k in _TOKEN_KEYS}
    return out if any(out.values()) else {}


def _result_bytes(entry: dict) -> int:
    """Rough size of a tool result as it was handed back to the model.

    A byte count, not a token count. The result is stored as parsed JSON, so
    re-serialising it is the closest honest measure available without
    re-tokenising every transcript on every run.
    """
    payload = entry.get("toolUseResult")
    if payload is None:
        return 0
    if isinstance(payload, str):
        return len(payload)
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def scan(
    paths: Optional[Iterable[Path]] = None,
    days: Optional[int] = None,
) -> Report:
    """Read local transcripts and attribute the token spend recorded in them."""
    report = Report()
    cutoff = time.time() - days * 86400 if days else None
    sessions: Dict[str, Session] = {}
    pending_tool: Dict[str, str] = {}  # tool_use id -> tool name

    for path in transcript_files(paths):
        try:
            if cutoff and path.stat().st_mtime < cutoff:
                continue
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            report.skipped += 1
            continue
        report.files += 1
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    _absorb(entry, report, sessions, pending_tool)

    report.sessions = sorted(
        sessions.values(), key=lambda s: s.requests, reverse=True
    )
    return report


def _absorb(
    entry: dict,
    report: Report,
    sessions: Dict[str, Session],
    pending_tool: Dict[str, str],
) -> None:
    kind = entry.get("type")
    if kind == "user":
        name = pending_tool.pop(_tool_use_id(entry), None)
        size = _result_bytes(entry)
        if name and size:
            _add(report.tool_result_bytes, name, size)
        return
    if kind != "assistant":
        return

    for block in (entry.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = str(block.get("name") or "?")
            _add(report.tool_calls, name, 1)
            if block.get("id"):
                pending_tool[str(block["id"])] = name

    server = entry.get("attributionMcpServer")
    if server:
        _add(report.mcp_calls, str(server), 1)

    usage = _usage(entry)
    if not usage:
        return
    report.requests += 1
    for key, value in usage.items():
        _add(report.totals, key, value)
    if entry.get("isSidechain"):
        report.subagent_requests += 1
        for key, value in usage.items():
            _add(report.subagent_totals, key, value)
        # A subagent's floor is its own, not the parent session's. Counting it
        # into the session would report a floor no request in that session
        # ever paid.
        return

    sid = str(entry.get("sessionId") or _fallback_id(entry))
    session = sessions.setdefault(sid, Session(id=sid))
    session.requests += 1
    session.date = str(entry.get("timestamp") or session.date)[:10]
    for key, value in usage.items():
        _add(session.tokens, key, value)
    whole_prompt = (
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["cache_read_input_tokens"]
    )
    session.floor = min(session.floor, whole_prompt) if session.floor else whole_prompt
    session.peak = max(session.peak, whole_prompt)


def _fallback_id(entry: dict) -> str:
    return str(entry.get("uuid") or "unknown")[:8]


def _tool_use_id(entry: dict) -> str:
    for block in (entry.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return str(block.get("tool_use_id") or "")
    return ""


# --------------------------------------------------------------------------- #
# Configured surface: what is loaded into every prompt whether or not it is used
# --------------------------------------------------------------------------- #
def configured_servers() -> Dict[str, str]:
    """Return MCP servers and plugins the agent loads, mapped to their source.

    Everything named here is paid for on every request of every session,
    because its tool schema sits in the prompt prefix. Whether it earns that
    is what `wn agent tools` answers.
    """
    found: Dict[str, str] = {}
    data = _read_json(Path.home() / ".claude.json")
    for name in (data.get("mcpServers") or {}):
        found[str(name)] = "~/.claude.json"
    for project, body in (data.get("projects") or {}).items():
        if not isinstance(body, dict):
            continue
        for name in (body.get("mcpServers") or {}):
            found.setdefault(str(name), "~/.claude.json (" + str(project) + ")")

    settings = _read_json(Path.home() / ".claude" / "settings.json")
    plugins = settings.get("enabledPlugins")
    if isinstance(plugins, dict):
        for name, enabled in plugins.items():
            if enabled:
                found.setdefault(str(name), "settings.json enabledPlugins")
    elif isinstance(plugins, list):
        for name in plugins:
            found.setdefault(str(name), "settings.json enabledPlugins")
    return found


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def namespace(tool_name: str) -> Optional[str]:
    """Return the MCP server a tool belongs to, or None for a built-in tool."""
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    return parts[1] if len(parts) > 2 else None


def canonical(name: str) -> str:
    """Fold the spellings one server appears under into a single key.

    The same server is attributed three ways in a transcript: a display name
    (Claude Browser), the same name with separators swapped (Claude_Browser),
    and the namespace inside a tool name (claude-in-chrome). Reporting those
    as three lines would triple-count the surface and make the idle list look
    shorter than it is.
    """
    folded = name.strip().casefold()
    return "".join(c if c.isalnum() else "_" for c in folded).strip("_")


def server_usage(report: Report) -> Tuple[Dict[str, int], List[str]]:
    """Split the configured surface into what was called and what was not."""
    raw: Dict[str, int] = dict(report.mcp_calls)
    for tool, calls in report.tool_calls.items():
        ns = namespace(tool)
        if ns:
            _add(raw, ns, calls)

    merged: Dict[str, int] = {}
    labels: Dict[str, str] = {}
    for name, calls in raw.items():
        key = canonical(name)
        merged[key] = merged.get(key, 0) + calls
        # Keep the spelling that was seen most often as the label, so the
        # report reads the way the agent names the server rather than the way
        # this function folds it.
        if calls >= raw.get(labels.get(key, ""), 0):
            labels[key] = name

    used = {labels[key]: calls for key, calls in merged.items()}
    idle = [name for name in configured_servers() if not _matches_any(name, used)]
    return used, sorted(idle)


def _matches_any(configured: str, used: Dict[str, int]) -> bool:
    """Match a configured name against an observed one.

    The two do not have to be spelled the same. A plugin is configured as
    engineering@claude-plugins-official and shows up in a transcript as
    engineering, or as a tool named mcp__plugin_engineering_github__something,
    so the comparison is on the part before the @ and is case-folded. A name
    that cannot be matched confidently is reported as idle, which is the
    conservative direction: it asks a question rather than disabling anything.
    """
    stem = configured.split("@")[0].strip().casefold()
    if not stem:
        return True
    for observed in used:
        seen = observed.strip().casefold()
        if stem == seen or stem in seen.replace("-", "_") or seen in stem:
            return True
    return False
