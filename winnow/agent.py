"""Agent-side accounting - the other half of the token bill.

Winnow began by compressing terminal output, which is the part of an agent's
context a hook can reach and rewrite. Measuring the result honestly showed how
small that part is. On the machine this was developed on, every output Winnow
ever compressed for Claude Code came to 236,177 tokens read. The same machine's
transcripts record 4.59 billion tokens of context read back across 14,955
billed requests. Terminal output was never where the money went.

This module reads what the agents already write to disk and says where the
money did go. Nothing here is sent anywhere, and nothing here is a price
estimate: the numbers are the token counts the provider returned, copied out of
the agent's own transcripts.

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

Three runtimes write three different transcripts, so there are three readers:

- **Claude Code** writes one JSONL per session under ``~/.claude/projects``,
  with the API usage counters on every assistant message. Full accounting.
- **Codex** writes rollout files under ``~/.codex/sessions``. Its counters
  arrive as their own ``token_count`` events and use different names, but they
  are complete. Full accounting.
- **Gemini in Antigravity** writes step transcripts under
  ``~/.gemini/antigravity/brain``. They carry no token counters of any kind:
  9,599 steps across 89 sessions on this machine and not one usage number.
  That is a limit of the data, not of this module, so Gemini is reported by
  activity and its token columns say so rather than showing a zero that would
  read as free.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CLAUDE = "claude"
CODEX = "codex"
GEMINI = "gemini"

# The counters Claude Code records. cache_read is the one that matters here:
# it is the prompt prefix being read again, and it is the only column that
# grows with the length of a session rather than with the work done in it.
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def transcript_roots() -> List[Path]:
    """Return every local transcript directory that exists.

    All three are files the agents already maintain for their own reasons.
    Winnow only reads them. WINNOW_TRANSCRIPTS overrides the list entirely.
    """
    override = os.environ.get("WINNOW_TRANSCRIPTS")
    if override:
        candidates = [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    else:
        candidates = [
            Path.home() / ".claude" / "projects",
            Path.home() / ".codex" / "sessions",
            Path.home() / ".gemini" / "antigravity" / "brain",
        ]
    return [p for p in candidates if p.is_dir()]


def transcript_files(roots: Optional[Iterable[Path]] = None) -> List[Path]:
    """Every transcript under the given roots, with duplicates dropped.

    Antigravity writes each session twice, as transcript.jsonl and
    transcript_full.jsonl with the same steps and only the field truncation
    differing. Reading both would double every count it produces, so the full
    one wins and its shorter twin is skipped.
    """
    paths: List[Path] = []
    for root in roots if roots is not None else transcript_roots():
        paths.extend(sorted(Path(root).rglob("*.jsonl")))
    full = {p.parent for p in paths if p.name == "transcript_full.jsonl"}
    return [p for p in paths if not (p.name == "transcript.jsonl" and p.parent in full)]


@dataclass
class Session:
    id: str
    date: str = ""
    requests: int = 0
    floor: int = 0  # smallest whole-prompt read seen: the fixed cost per turn
    peak: int = 0
    tokens: Dict[str, int] = field(default_factory=dict)


@dataclass
class Runtime:
    """One agent's slice of the report.

    ``tokens_available`` is false for a runtime whose transcripts carry no
    usage counters. Every token column on such a runtime is unknown rather
    than zero, and the two must never be printed the same way.
    """

    name: str
    files: int = 0
    requests: int = 0
    steps: int = 0
    tokens_available: bool = True
    totals: Dict[str, int] = field(default_factory=dict)
    tool_calls: Dict[str, int] = field(default_factory=dict)
    sessions: List[Session] = field(default_factory=list)

    @property
    def floor(self) -> int:
        floors = [s.floor for s in self.sessions if s.floor]
        return int(statistics.median(floors)) if floors else 0

    @property
    def fixed_cost(self) -> int:
        return self.floor * self.requests

    @property
    def context_read(self) -> int:
        return self.totals.get("cache_read_input_tokens", 0)


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
    runtimes: Dict[str, Runtime] = field(default_factory=dict)
    unreadable: int = 0
    unrecognised: int = 0

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
        """Tokens spent re-reading the floor, across every request counted.

        Summed per runtime rather than taken from the blended median. Claude
        and Codex sit at different floors, so multiplying one median by the
        combined request count would report a cost neither runtime paid.
        """
        per_runtime = [
            rt.fixed_cost for rt in self.runtimes.values() if rt.tokens_available
        ]
        return sum(per_runtime) if per_runtime else self.floor * self.requests

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


# --------------------------------------------------------------------------- #
# format detection
# --------------------------------------------------------------------------- #
def detect(entry: dict) -> Optional[str]:
    """Name the runtime that wrote a transcript line, or None if unclear.

    Detection is on shape rather than on path, so a transcript copied
    elsewhere or pointed at through WINNOW_TRANSCRIPTS still reads correctly.
    """
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind in ("session_meta", "response_item", "event_msg", "turn_context"):
        return CODEX
    if "step_index" in entry and "created_at" in entry:
        return GEMINI
    if kind in ("assistant", "user", "system", "attachment") and (
        "sessionId" in entry or "message" in entry
    ):
        return CLAUDE
    return None


def _sniff(lines: Iterable[str]) -> Tuple[Optional[str], List[dict]]:
    """Read far enough into a file to name its format, keeping what was read."""
    seen: List[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        seen.append(entry)
        runtime = detect(entry)
        if runtime:
            return runtime, seen
        if len(seen) >= 20:
            break
    return None, seen


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def scan(
    paths: Optional[Iterable[Path]] = None,
    days: Optional[int] = None,
) -> Report:
    """Read local transcripts and attribute the token spend recorded in them."""
    report = Report()
    cutoff = time.time() - days * 86400 if days else None
    state: Dict[str, Dict[str, Session]] = {CLAUDE: {}, CODEX: {}, GEMINI: {}}
    pending_tool: Dict[str, str] = {}  # tool_use id -> tool name

    for path in transcript_files(paths):
        try:
            if cutoff and path.stat().st_mtime < cutoff:
                continue
            text = path.open(encoding="utf-8", errors="replace")
        except OSError:
            report.unreadable += 1
            continue
        with text:
            runtime, head = _sniff(text)
            if runtime is None:
                report.unrecognised += 1
                continue
            slice_ = report.runtimes.setdefault(runtime, Runtime(name=runtime))
            slice_.files += 1
            report.files += 1
            absorb = _ABSORBERS[runtime]
            for entry in head:
                absorb(entry, report, slice_, state[runtime], pending_tool, path)
            for line in text:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    absorb(entry, report, slice_, state[runtime], pending_tool, path)

    for runtime, sessions in state.items():
        if runtime in report.runtimes:
            report.runtimes[runtime].sessions = sorted(
                sessions.values(), key=lambda s: s.requests, reverse=True
            )
    report.sessions = sorted(
        (s for sessions in state.values() for s in sessions.values()),
        key=lambda s: s.requests,
        reverse=True,
    )
    return report


def _record(
    slice_: Runtime,
    sessions: Dict[str, Session],
    session_id: str,
    usage: Dict[str, int],
    prompt_total: int,
    date: str,
) -> None:
    """Fold one billed request into its session and its runtime."""
    session = sessions.setdefault(session_id, Session(id=session_id))
    session.requests += 1
    if date:
        session.date = date[:10]
    for key, value in usage.items():
        _add(session.tokens, key, value)
    if prompt_total:
        session.floor = min(session.floor, prompt_total) if session.floor else prompt_total
        session.peak = max(session.peak, prompt_total)
    slice_.requests += 1
    for key, value in usage.items():
        _add(slice_.totals, key, value)


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
def _usage_claude(entry: dict) -> Dict[str, int]:
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


def _tool_use_id(entry: dict) -> str:
    for block in (entry.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return str(block.get("tool_use_id") or "")
    return ""


def _absorb_claude(entry, report, slice_, sessions, pending_tool, path) -> None:
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
            _add(slice_.tool_calls, name, 1)
            if block.get("id"):
                pending_tool[str(block["id"])] = name

    server = entry.get("attributionMcpServer")
    if server:
        _add(report.mcp_calls, str(server), 1)

    usage = _usage_claude(entry)
    if not usage:
        return
    report.requests += 1
    for key, value in usage.items():
        _add(report.totals, key, value)
    if entry.get("isSidechain"):
        report.subagent_requests += 1
        slice_.requests += 1
        for key, value in usage.items():
            _add(report.subagent_totals, key, value)
            _add(slice_.totals, key, value)
        # A subagent's floor is its own, not the parent session's. Counting it
        # into the session would report a floor no request in that session
        # ever paid.
        return

    _record(
        slice_,
        sessions,
        str(entry.get("sessionId") or str(entry.get("uuid") or "unknown")[:8]),
        usage,
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["cache_read_input_tokens"],
        str(entry.get("timestamp") or ""),
    )


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #
def _absorb_codex(entry, report, slice_, sessions, pending_tool, path) -> None:
    """Read a Codex rollout file.

    Codex reports usage in its own events rather than on each message, and
    names the columns differently: ``cached_input_tokens`` is the part of the
    prompt served from cache, and ``input_tokens`` is the whole prompt with
    the cached part already inside it. Adding them the way Claude's columns
    add would count the prefix twice, so the prompt total is taken from
    input_tokens alone and the cached figure is mapped onto the shared
    cache_read column for comparison.
    """
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return
    kind = payload.get("type")

    if kind in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or payload.get("tool_name") or "?")
        _add(report.tool_calls, name, 1)
        _add(slice_.tool_calls, name, 1)
        return
    if kind in ("function_call_output", "custom_tool_call_output"):
        text = payload.get("output")
        if isinstance(text, (str, bytes)):
            _add(report.tool_result_bytes, "codex tool output", len(text))
        return
    if kind != "token_count":
        return

    info = payload.get("info")
    if not isinstance(info, dict):
        return
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return
    prompt = int(last.get("input_tokens") or 0)
    cached = int(last.get("cached_input_tokens") or 0)
    output = int(last.get("output_tokens") or 0)
    reasoning = int(last.get("reasoning_output_tokens") or 0)
    if not (prompt or output):
        return
    usage = {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": output + reasoning,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }
    report.requests += 1
    for key, value in usage.items():
        _add(report.totals, key, value)
    _record(
        slice_,
        sessions,
        str(path),
        usage,
        prompt,
        str(entry.get("timestamp") or ""),
    )


# --------------------------------------------------------------------------- #
# Gemini in Antigravity
# --------------------------------------------------------------------------- #
def _absorb_gemini(entry, report, slice_, sessions, pending_tool, path) -> None:
    """Read an Antigravity step transcript.

    There is nothing to bill here. These files record steps, tool calls and
    their exit codes, and no usage counter of any kind, so this counts
    activity and leaves every token column unknown. A zero would read as free.
    """
    slice_.tokens_available = False
    slice_.steps += 1
    for call in entry.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("tool_name") or call.get("name") or "?")
        _add(report.tool_calls, name, 1)
        _add(slice_.tool_calls, name, 1)


_ABSORBERS = {
    CLAUDE: _absorb_claude,
    CODEX: _absorb_codex,
    GEMINI: _absorb_gemini,
}


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

    codex = _read_json(Path.home() / ".codex" / "mcp.json")
    for name in (codex.get("mcpServers") or {}):
        found.setdefault(str(name), "~/.codex/mcp.json")
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
