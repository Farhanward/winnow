"""Declarative rule engine (the customizable middle layer).

Rules live in YAML so anyone can tune Winnow to their own tools without touching
Python. Each rule matches a command with a regex and runs an ordered list of
actions over the output. Built-in rule packs ship with the package; user rule
packs in ``$WINNOW_HOME/rules/*.yaml`` are loaded on top and win ties by being
applied last.

Rule shape::

    - name: drop-npm-noise
      match: "npm (install|ci)"     # regex, tested against the command
      stop: false                    # optional: stop the pipeline after this rule
      actions:
        - strip_ansi: true
        - drop_lines: "^npm (warn|notice)"
        - collapse_repeats: 3
        - keep_head_tail: [40, 20]
        - max_line_len: 500
        - summary: "hid {dropped} noise lines"

Supported actions: ``drop_lines`` (regex), ``keep_lines`` (regex),
``replace`` ([pattern, repl]), ``collapse_repeats`` (bool|int),
``cascade_guard`` (bool|int), ``keep_head_tail`` ([head, tail]),
``max_line_len`` (int), ``strip_ansi`` (bool), ``summary`` (str with
``{dropped}`` / ``{replaced}`` placeholders).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from . import config, patterns

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PACKAGE_RULES = Path(__file__).parent / "rules_data"


def load_rules() -> List[Dict[str, Any]]:
    """Load built-in rule packs, then user rule packs layered on top."""
    rules: List[Dict[str, Any]] = []
    for source in (_PACKAGE_RULES, config.user_rules_dir()):
        if not source.exists():
            continue
        for path in sorted(source.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
                if isinstance(data, list):
                    rules.extend(r for r in data if isinstance(r, dict))
            except (yaml.YAMLError, OSError):
                continue
    return rules


_CLIP_MARKER = re.compile(r"… ⟨\+(\d+) chars⟩$")


def _unclipped(line: str):
    """Split a line into its body and the characters a previous clip dropped.

    Rules clip independently, so the same line can pass through more than one
    of them. Without this the last clip overwrites the earlier count and the
    line claims to be missing far less than it actually is.
    """
    match = _CLIP_MARKER.search(line)
    if not match:
        return line, 0
    return line[: match.start()], int(match.group(1))


def apply_rules(
    command: str, text: str, rules: List[Dict[str, Any]], cfg
) -> Tuple[str, List[str]]:
    """Apply every matching rule in order. Returns (text, applied_rule_names)."""
    applied: List[str] = []
    for rule in rules:
        match = rule.get("match")
        if not match:
            continue
        try:
            if not re.search(match, command, re.IGNORECASE):
                continue
        except re.error:
            continue
        before = text
        text = _apply_actions(rule, text, cfg)
        if text != before:
            applied.append(rule.get("name", "unnamed"))
        if rule.get("stop"):
            break
    return text, applied


def _apply_actions(rule: Dict[str, Any], text: str, cfg) -> str:
    stats = {"dropped": 0, "replaced": 0}
    for action in rule.get("actions", []) or []:
        if not isinstance(action, dict):
            continue
        for key, val in action.items():
            text = _run_action(key, val, text, cfg, stats)
    return text


def _run_action(key: str, val: Any, text: str, cfg, stats: Dict[str, int]) -> str:
    if key == "strip_ansi" and val:
        return _ANSI.sub("", text)

    if key == "drop_lines":
        try:
            pat = re.compile(val)
        except re.error:
            return text
        kept = []
        for ln in text.split("\n"):
            if pat.search(ln):
                stats["dropped"] += 1
            else:
                kept.append(ln)
        return "\n".join(kept)

    if key == "keep_lines":
        try:
            pat = re.compile(val)
        except re.error:
            return text
        kept = []
        for ln in text.split("\n"):
            if pat.search(ln):
                kept.append(ln)
            else:
                stats["dropped"] += 1
        return "\n".join(kept)

    if key == "replace" and isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            new, n = re.subn(val[0], val[1], text)
        except re.error:
            return text
        stats["replaced"] += n
        return new

    if key == "collapse_repeats" and val:
        threshold = val if isinstance(val, int) and val > 1 else cfg.collapse_threshold
        new, removed = patterns.collapse_repeats(text, threshold)
        stats["dropped"] += removed
        return new

    if key == "limit_per_file" and val:
        keep = val if isinstance(val, int) and val > 0 else 5
        new, removed = patterns.limit_per_file(text, keep)
        stats["dropped"] += removed
        return new

    if key == "cascade_guard" and val:
        limit = val if isinstance(val, int) and val > 0 else 5
        new, removed = patterns.cascade_guard(text, limit)
        stats["dropped"] += removed
        return new

    if key == "keep_head_tail" and isinstance(val, (list, tuple)) and len(val) == 2:
        lines, hidden = patterns.keep_ends(
            text.split("\n"), int(val[0]), int(val[1]), "lines"
        )
        stats["dropped"] += hidden
        return "\n".join(lines)

    if key == "max_line_len":
        n = int(val)
        out = []
        for ln in text.split("\n"):
            body, already = _unclipped(ln)
            if len(body) <= n:
                # Covers a line an earlier rule already clipped tighter than
                # this one asks for. Re-clipping would move the marker without
                # dropping anything.
                out.append(ln)
                continue
            # The count is cumulative. Two rules can clip the same line, the
            # generic 800 and a command rule at 300, and reporting only the
            # second pass told the reader 515 characters were missing when
            # 1,714 were. A marker that undercounts is worse than no marker:
            # it invites the reader to treat a fragment as nearly whole.
            dropped = len(body) - n + already
            out.append(body[:n] + f"… ⟨+{dropped} chars⟩")
        return "\n".join(out)

    if key == "summary" and isinstance(val, str):
        try:
            return text + "\n" + val.format(**stats)
        except (KeyError, IndexError, ValueError):
            return text + "\n" + val

    return text
