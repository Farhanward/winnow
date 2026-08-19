"""Command-line interface for Winnow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import List, Optional

from . import __version__, agent, core, efficiency, hook, reads
from . import rules as rules_mod
from . import semantic, tokens
from .filters import all_filters
from .store import Store, is_handle


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# run: execute a command and compress its output
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    argv = list(args.rest)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        _print("winnow: nothing to run (usage: wn run -- <command>)")
        return 2

    command = " ".join(argv)
    try:
        if args.shell:
            proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        else:
            try:
                proc = subprocess.run(argv, capture_output=True, text=True)
            except (FileNotFoundError, NotADirectoryError, OSError):
                proc = subprocess.run(command, shell=True, capture_output=True,
                                      text=True)
    except Exception as exc:  # pragma: no cover - defensive
        _print(f"winnow: failed to run command: {exc}")
        return 1

    raw = proc.stdout or ""
    if proc.stderr:
        raw = (raw + "\n" + proc.stderr) if raw else proc.stderr

    store = None if args.no_remember else Store()
    started = time.perf_counter_ns()
    result = core.compress(
        command=command,
        raw=raw,
        store=store,
        remember=not args.no_remember,
        exit_code=proc.returncode,
        cwd=os.getcwd(),
    )
    process_ns = time.perf_counter_ns() - started
    rendered = result.render(footer=not args.no_footer)
    client = efficiency.detect_client(getattr(args, "client", None))
    efficiency.record_run(
        client,
        result.raw_tokens,
        tokens.estimate(rendered),
        not result.passthrough,
        process_ns,
    )
    _print(rendered)
    if store:
        store.close()
    return proc.returncode


# --------------------------------------------------------------------------- #
# filter: compress text piped in on stdin
# --------------------------------------------------------------------------- #
def cmd_filter(args) -> int:
    raw = sys.stdin.read()
    store = None if args.no_remember else Store()
    started = time.perf_counter_ns()
    result = core.compress(
        command=args.cmd or "",
        raw=raw,
        store=store,
        remember=not args.no_remember,
        cwd=os.getcwd(),
    )
    process_ns = time.perf_counter_ns() - started
    rendered = result.render(footer=not args.no_footer)
    client = efficiency.detect_client(getattr(args, "client", None))
    efficiency.record_run(
        client,
        result.raw_tokens,
        tokens.estimate(rendered),
        not result.passthrough,
        process_ns,
    )
    _print(rendered)
    if store:
        store.close()
    return 0


# --------------------------------------------------------------------------- #
# recall: fetch a stored full output by handle, or search history
# --------------------------------------------------------------------------- #
def cmd_recall(args) -> int:
    store = Store()
    query = " ".join(args.query)
    try:
        if is_handle(query):
            rec = store.get(query)
            if not rec:
                _print(f"winnow: no output with handle '{query}'")
                return 1
            text = rec.raw
            if args.lines:
                text = _slice_lines(text, args.lines)
            _print(text)
            return 0
        # Otherwise treat as a full-text search over stored outputs.
        hits = store.search(query, limit=args.limit)
        if not hits:
            _print(f"winnow: no stored output matches '{query}'")
            return 1
        for rec in hits:
            snippet = " ".join(rec.raw.split())[:160]
            _print(f"[{rec.id}] {rec.command[:70]}")
            _print(f"        {snippet}")
        return 0
    finally:
        store.close()


def _slice_lines(text: str, spec: str) -> str:
    lines = text.split("\n")
    try:
        if "-" in spec:
            a, b = spec.split("-", 1)
            start = int(a) - 1 if a else 0
            end = int(b) if b else len(lines)
        else:
            start = int(spec) - 1
            end = start + 1
    except ValueError:
        return text
    return "\n".join(lines[max(0, start):end])


# --------------------------------------------------------------------------- #
# gain: savings analytics
# --------------------------------------------------------------------------- #
def cmd_gain(args) -> int:
    store = Store()
    t = store.totals()
    exact = "exact (tiktoken)" if tokens.using_exact() else "estimated (heuristic)"
    _print("─" * 52)
    _print(f"  winnow savings · {t['count']} outputs compressed")
    _print("─" * 52)
    _print(f"  tokens in    : {t['raw']:>12,}")
    _print(f"  tokens out   : {t['comp']:>12,}")
    _print(f"  tokens saved : {t['saved']:>12,}")
    _print(f"  reduction    : {t['pct']:>11.1f}%")
    _print(f"  counting     : {exact}")
    _print("─" * 52)
    if args.history:
        _print("  recent:")
        for rec in store.recent(limit=args.limit):
            saved = rec.raw_tokens - rec.comp_tokens
            pct = (saved / rec.raw_tokens * 100) if rec.raw_tokens else 0
            _print(f"    [{rec.id}] {pct:5.0f}%  "
                   f"{rec.raw_tokens:>6}→{rec.comp_tokens:<6}  "
                   f"{rec.filt[:22]:22}  {rec.command[:40]}")
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# efficiency: aggregate-only per-runtime measurements
# --------------------------------------------------------------------------- #
def cmd_efficiency(args) -> int:
    collector = efficiency.Collector()
    rows = collector.snapshot()
    collector.close()

    data = {}
    for client, row in rows.items():
        if client == "unknown" and not row.runs and not row.observed:
            continue
        data[client] = {
            "observed": row.observed,
            "auto_selected": row.selected,
            # Read/Grep inputs the hook capped. Reported next to the
            # compression figures, never inside them: a clamp caps a request,
            # it does not compress an output.
            "inputs_seen": row.inputs_seen,
            "inputs_clamped": row.inputs_clamped,
            "inputs_clamped_pct": round(row.clamp_pct, 1),
            "runs": row.runs,
            "compressed": row.compressed,
            "passthrough": row.passthrough,
            "tokens_in": row.raw_tokens,
            "tokens_out": row.output_tokens,
            "tokens_saved": row.saved_tokens,
            "reduction_pct": round(row.reduction_pct, 1),
            "average_process_ms": round(row.average_process_ms, 3),
            "updated_at": row.updated_at or None,
        }
    if args.json:
        _print(json.dumps(data, indent=2))
        return 0

    _print("winnow efficiency · aggregate counters only")
    _print(
        "runtime     seen/auto   runs  compressed   tokens in→out"
        "     saved  clamped  last update"
    )
    for client, row in rows.items():
        if client == "unknown" and not row.runs and not row.observed:
            continue
        last = (
            time.strftime("%Y-%m-%d", time.localtime(row.updated_at))
            if row.updated_at else "-"
        )
        clamped = f"{row.inputs_clamped:>4}/{row.inputs_seen:<4}"
        if not row.runs:
            _print(
                f"{client:<11} {row.observed:>4}/{row.selected:<4} "
                f"{row.runs:>6} {row.compressed:>7}  no data"
                f"{'':>18} {clamped} {last}"
            )
            continue
        _print(
            f"{client:<11} {row.observed:>4}/{row.selected:<4} "
            f"{row.runs:>6} {row.compressed:>7}  "
            f"{row.raw_tokens:>8,}→{row.output_tokens:<8,} "
            f"{row.reduction_pct:>6.1f}%  {clamped} {last}"
        )
    return 0


# --------------------------------------------------------------------------- #
# discover: find the biggest un-compressed outputs still in the store
# --------------------------------------------------------------------------- #
def cmd_discover(args) -> int:
    store = Store()
    recs = sorted(store.recent(limit=500), key=lambda r: r.raw_tokens, reverse=True)
    _print("Largest outputs seen (biggest token sinks first):")
    for rec in recs[: args.limit]:
        _print(f"  {rec.raw_tokens:>7,} tok  [{rec.id}]  {rec.command[:60]}")
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# skim: structural skeleton of a source file
# --------------------------------------------------------------------------- #
def cmd_skim(args) -> int:
    try:
        source = open(args.file, "r", encoding="utf-8", errors="replace").read()
    except OSError as exc:
        _print(f"winnow: cannot read {args.file}: {exc}")
        return 1
    skeleton = None
    if args.file.endswith(".py"):
        skeleton = semantic.skim_python(source)
    if skeleton is None:
        js = semantic.compress_json(source)
        if js is not None:
            skeleton = js[0]
    if skeleton is None:
        _print("winnow: no structural skimmer for this file type "
               "(supported: .py, .json)")
        return 1
    before, after = tokens.estimate(source), tokens.estimate(skeleton)
    pct = (before - after) / before * 100 if before else 0
    _print(skeleton)
    _print(f"⟨winnow skim: {before}→{after} tok, saved {pct:.0f}%⟩")
    return 0


# --------------------------------------------------------------------------- #
# rules: inspect the rule engine
# --------------------------------------------------------------------------- #
def cmd_rules(args) -> int:
    from .config import user_rules_dir

    if args.action == "path":
        _print(str(user_rules_dir()))
        return 0
    rules = rules_mod.load_rules()
    if args.action == "test":
        matched = [r.get("name", "?") for r in rules
                   if r.get("match") and _safe_search(r["match"], args.cmd or "")]
        _print(f"command: {args.cmd!r}")
        _print(f"built-in filter: {_filter_for(args.cmd or '')}")
        _print("matching rules: " + (", ".join(matched) if matched else "(none)"))
        return 0
    # default: list
    _print(f"built-in filters: {', '.join(all_filters())}")
    _print(f"rules loaded    : {len(rules)}")
    for r in rules:
        _print(f"  - {r.get('name', '?'):24} match={r.get('match', '')!r}")
    return 0


def _safe_search(pattern: str, text: str) -> bool:
    import re
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return False


def _filter_for(command: str) -> str:
    from .filters import detect
    _, name = detect(command)
    return name or "(none)"


# --------------------------------------------------------------------------- #
# hook: Claude Code / agent integration
# --------------------------------------------------------------------------- #
def cmd_hook(args) -> int:
    if args.action == "run":
        return hook.run_hook()
    if args.action == "show":
        _print(json.dumps(hook.settings_snippet(), indent=2))
        _print("")
        _print("Add the above to your Claude Code settings.json, or use the "
               "prefix form directly:  wn run -- <command>")
        return 0
    if args.action == "install":
        return _hook_install(args.settings)
    return 2


def _hook_install(settings_path: Optional[str]) -> int:
    path = settings_path or os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json")
    snippet = hook.settings_snippet()
    data = {}
    if os.path.exists(path):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (json.JSONDecodeError, OSError):
            data = {}
    hooks = data.setdefault("hooks", {})
    # Every matcher of every event, not just the first matcher of the first
    # event. Installing entry [0] alone left the PowerShell matcher out of the
    # settings file the whole time the README said it went in; reading only
    # PreToolUse would now leave out the compaction events the read ledger
    # needs to stay correct.
    for event, entries in snippet["hooks"].items():
        existing = hooks.setdefault(event, [])
        for entry in entries:
            if not any(json.dumps(e) == json.dumps(entry) for e in existing):
                existing.append(entry)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2))
    _print(f"winnow: hook installed to {path}")
    return 0


# --------------------------------------------------------------------------- #
# agent: where the token budget actually goes, and what to do about it
# --------------------------------------------------------------------------- #
def cmd_agent(args) -> int:
    if args.action == "audit":
        return _agent_audit(args)
    if args.action == "tools":
        return _agent_tools(args)
    return 2


def _runtime_rows(report):
    """One row per runtime whose transcripts were found.

    A runtime with no usage counters in its transcripts prints dashes, never
    zeros. Antigravity records steps and tool calls and no token of any kind,
    and a zero in a token column would read as free rather than as unknown.
    """
    rows = []
    for name, rt in sorted(report.runtimes.items()):
        if not rt.tokens_available:
            rows.append(
                f"  {name:<9} {rt.files:>5}  {rt.steps:>8,}  "
                f"{'no counters':>17}  {'-':>9}  {'-':>13}"
            )
            continue
        rows.append(
            f"  {name:<9} {rt.files:>5}  {rt.requests:>8,}  "
            f"{rt.context_read:>17,}  {rt.floor:>9,}  {rt.fixed_cost:>13,}"
        )
    return rows


def _agent_audit(args) -> int:
    report = agent.scan(days=args.days)
    if not report.files:
        _print("winnow: no agent transcripts found.")
        _print("Looked in ~/.claude/projects and ~/.codex/sessions. Set "
               "WINNOW_TRANSCRIPTS to point somewhere else.")
        return 0

    tools = sorted(report.tool_calls.items(), key=lambda kv: -kv[1])
    heavy = sorted(report.tool_result_bytes.items(), key=lambda kv: -kv[1])
    if args.json:
        _print(json.dumps({
            "files": report.files,
            "sessions": len(report.sessions),
            "requests": report.requests,
            "subagent_requests": report.subagent_requests,
            "tokens": report.totals,
            "subagent_tokens": report.subagent_totals,
            "session_floor": report.floor,
            "floor_cost": report.fixed_cost,
            "subagent_share_pct": round(report.subagent_share, 1),
            "tool_calls": dict(tools),
            "tool_result_bytes": dict(heavy),
            "mcp_calls": report.mcp_calls,
            "runtimes": {
                name: {
                    "files": rt.files,
                    "requests": rt.requests,
                    "steps": rt.steps,
                    "tokens_available": rt.tokens_available,
                    "tokens": rt.totals,
                    "session_floor": rt.floor,
                    "floor_cost": rt.fixed_cost,
                }
                for name, rt in sorted(report.runtimes.items())
            },
            "unreadable_files": report.unreadable,
            "unrecognised_files": report.unrecognised,
        }, indent=2))
        return 0

    window = f"last {args.days} days" if args.days else "all transcripts"
    _print("─" * 62)
    _print(f"  winnow agent audit · {window}")
    _print("─" * 62)
    _print(f"  sessions          : {len(report.sessions):>14,}")
    _print(f"  billed requests   : {report.requests:>14,}")
    _print(f"  context re-read   : {report.context_read:>14,}")
    _print(f"  new context       : "
           f"{report.totals.get('cache_creation_input_tokens', 0):>14,}")
    _print(f"  output            : "
           f"{report.totals.get('output_tokens', 0):>14,}")
    _print("─" * 62)
    _print(f"  median floor      : {report.floor:>14,}   paid on every request")
    _print(f"  floor cost        : {report.fixed_cost:>14,}   summed per runtime")
    _print(f"  subagent requests : {report.subagent_requests:>14,}   "
           f"{report.subagent_share:.1f}% of context read")
    _print("─" * 62)

    _print("  runtime    files  requests    context re-read      floor    "
           "floor cost")
    _print("  " + "─" * 58)
    for row in _runtime_rows(report):
        _print(row)
    _print("─" * 62)

    if tools:
        _print("  most-called tools:")
        for name, calls in tools[:6]:
            _print(f"    {calls:>6,}  {name}")
    if heavy:
        _print("  largest results returned (bytes):")
        for name, size in heavy[:6]:
            _print(f"    {size:>12,}  {name}")
    _print("")
    _print("The floor is prompt prefix: system prompt, memory files, skill "
           "descriptions,")
    _print("and every connected MCP server's tool schema. Run `wn agent "
           "tools` to see")
    _print("which of those were never called.")
    return 0


def _agent_tools(args) -> int:
    report = agent.scan(days=args.days)
    used, idle = agent.server_usage(report)
    configured = agent.configured_servers()

    if args.json:
        _print(json.dumps({
            "configured": configured,
            "called": used,
            "idle": idle,
            "session_floor": report.floor,
            "requests": report.requests,
        }, indent=2))
        return 0

    if not configured and not used:
        _print("winnow: no MCP servers or plugins found in the agent config.")
        return 0

    _print("winnow agent tools · what the prompt prefix carries")
    _print("")
    if used:
        _print("called at least once:")
        for name, calls in sorted(used.items(), key=lambda kv: -kv[1]):
            source = configured.get(name, "not in config")
            _print(f"  {calls:>6,}  {name}   ({source})")
    if idle:
        _print("")
        _print("configured but never called in the transcripts read:")
        for name in idle:
            _print(f"          {name}   ({configured.get(name, '?')})")
        _print("")
        _print(f"Each one ships its tool schema in every request. The floor "
               f"here is {report.floor:,}")
        _print(f"tokens across {report.requests:,} requests. Disabling a "
               f"server you do not call")
        _print("lowers that floor for every request after it, and re-enabling "
               "it is one edit.")
        _print("")
        _print("Winnow does not edit your agent config. The names above are "
               "the ones to")
        _print("remove from enabledPlugins or mcpServers if you agree.")
    else:
        _print("")
        _print("Every configured server was called. Nothing idle to trim.")
    return 0


# --------------------------------------------------------------------------- #
# reads: what the hook believes the agent has already been shown
# --------------------------------------------------------------------------- #
def cmd_reads(args) -> int:
    """Inspect or reset the repeat-read ledger.

    A suppression the user cannot inspect is a suppression they cannot trust,
    which is the whole reason this command exists.
    """
    ledger = reads.Ledger()
    try:
        if args.action == "clear":
            removed = ledger.conn.execute("DELETE FROM reads").rowcount
            ledger.conn.commit()
            _print(f"winnow: cleared {removed} remembered read(s)")
            return 0

        rows = ledger.conn.execute(
            "SELECT session, key, served_at, suppressed FROM reads "
            "ORDER BY served_at DESC"
        ).fetchall()
    finally:
        ledger.close()

    if args.json:
        _print(json.dumps([
            {
                "session": r[0],
                "path": r[1].split("|")[0],
                "offset": r[1].split("|")[1],
                "limit": r[1].split("|")[2],
                "served_at": r[2],
                "suppressed": bool(r[3]),
            }
            for r in rows
        ], indent=2))
        return 0

    if not rows:
        _print("winnow: the read ledger is empty.")
        return 0

    suppressed = sum(1 for r in rows if r[3])
    sessions = len({r[0] for r in rows})
    _print(f"winnow reads · {len(rows)} remembered across {sessions} session(s), "
           f"{suppressed} already suppressed once")
    for session, key, served_at, was_suppressed in rows[: args.limit]:
        path = key.split("|")[0]
        mark = "suppressed" if was_suppressed else "served"
        when = reads.elapsed(time.time() - float(served_at or 0))
        _print(f"  {mark:<11} {when:<16} {os.path.basename(path)[:40]:<42} "
               f"{session[:8]}")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="winnow",
        description="Winnow the noise out of your CLI output — keep the grain, "
                    "stash the chaff, recall it anytime.",
    )
    p.add_argument("-V", "--version", action="version",
                   version=f"winnow {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run a command and compress its output")
    r.add_argument("--shell", action="store_true", help="run via the shell")
    r.add_argument("--no-remember", action="store_true",
                   help="do not store the full output for recall")
    r.add_argument("--no-footer", action="store_true",
                   help="suppress the savings/recall footer")
    r.add_argument("--client", choices=efficiency.CLIENTS,
                   help="runtime label for aggregate efficiency metrics")
    r.add_argument("rest", nargs=argparse.REMAINDER,
                   help="-- <command and args>")
    r.set_defaults(func=cmd_run)

    f = sub.add_parser("filter", help="compress text piped on stdin")
    f.add_argument("--cmd", default="", help="label the source command "
                   "(drives which filters/rules apply)")
    f.add_argument("--no-remember", action="store_true")
    f.add_argument("--no-footer", action="store_true")
    f.add_argument("--client", choices=efficiency.CLIENTS,
                   help="runtime label for aggregate efficiency metrics")
    f.set_defaults(func=cmd_filter)

    rc = sub.add_parser("recall", help="fetch a stored output by handle, or search")
    rc.add_argument("query", nargs="+", help="handle (e.g. a1b2c3) or search text")
    rc.add_argument("--lines", help="line range for a handle, e.g. 10-40")
    rc.add_argument("--limit", type=int, default=10)
    rc.set_defaults(func=cmd_recall)

    g = sub.add_parser("gain", help="show token savings analytics")
    g.add_argument("--history", action="store_true", help="list recent outputs")
    g.add_argument("--limit", type=int, default=20)
    g.set_defaults(func=cmd_gain)

    e = sub.add_parser(
        "efficiency", help="show aggregate efficiency by agent runtime"
    )
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_efficiency)

    d = sub.add_parser("discover", help="find the biggest token sinks seen")
    d.add_argument("--limit", type=int, default=15)
    d.set_defaults(func=cmd_discover)

    s = sub.add_parser("skim", help="structural skeleton of a .py/.json file")
    s.add_argument("file")
    s.set_defaults(func=cmd_skim)

    ru = sub.add_parser("rules", help="inspect the rule engine")
    ru.add_argument("action", nargs="?", default="list",
                    choices=["list", "path", "test"])
    ru.add_argument("--cmd", help="command to test rule matching against")
    ru.set_defaults(func=cmd_rules)

    ag = sub.add_parser("agent", help="audit and tune the agent's own token budget")
    ag.add_argument("action", choices=["audit", "tools"])
    ag.add_argument("--days", type=int, default=None,
                    help="only read transcripts touched in the last N days")
    ag.add_argument("--json", action="store_true", help="machine-readable output")
    ag.set_defaults(func=cmd_agent)

    rd = sub.add_parser("reads", help="inspect the repeat-read ledger")
    rd.add_argument("action", nargs="?", default="stats", choices=["stats", "clear"])
    rd.add_argument("--limit", type=int, default=20, help="rows to show")
    rd.add_argument("--json", action="store_true", help="machine-readable output")
    rd.set_defaults(func=cmd_reads)

    h = sub.add_parser("hook", help="Claude Code / agent integration")
    h.add_argument("action", choices=["show", "run", "install"])
    h.add_argument("--settings", help="path to settings.json for install")
    h.set_defaults(func=cmd_hook)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
