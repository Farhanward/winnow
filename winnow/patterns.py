"""Pattern collapsing — turn repetitive noise into fingerprints.

Logs, installers and test runners emit long runs of near-identical lines that
differ only in a number, a hash, a path or a timestamp. Winnow normalises each
line into a *fingerprint*, then collapses consecutive runs of the same
fingerprint into a single representative line annotated with a repeat count.
This is how a 900-line install log becomes a dozen lines without losing the
shape of what happened.
"""

from __future__ import annotations

import re
from typing import Tuple

# Order matters: most specific first so a UUID isn't eaten by the number rule.
_NORMALISERS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{40}\b", re.I), "<sha1>"),
    (re.compile(r"\b[0-9a-f]{64}\b", re.I), "<sha256>"),
    (re.compile(r"\b[0-9a-f]{7,12}\b", re.I), "<hash>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "<ts>"),
    (re.compile(r"\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "<time>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<addr>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|kb|mb|gb|b)\b", re.I), "<size>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]


def fingerprint(line: str) -> str:
    """Collapse volatile fields so near-identical lines share a fingerprint."""
    s = line.rstrip()
    for pat, repl in _NORMALISERS:
        s = pat.sub(repl, s)
    return s.strip()


def keep_ends(lines, head: int, tail: int, label: str = "lines"):
    """Keep the first `head` and last `tail` lines, noting what went.

    One implementation for both callers. The built-in filters and the
    `keep_head_tail` rule action had a copy each, identical but for the word in
    the marker, which is the shape a divergence starts as.

    Returns `(lines, hidden)` so a caller that keeps statistics can record the
    drop without counting the lines a second time.
    """
    head = max(0, int(head))
    tail = max(0, int(tail))
    if len(lines) <= head + tail + 1:
        return list(lines), 0
    hidden = len(lines) - head - tail
    kept = list(lines[:head]) + [f"… ⟨{hidden} {label} hidden⟩"]
    kept.extend(lines[len(lines) - tail:] if tail else [])
    return kept, hidden


def collapse_repeats(text: str, threshold: int = 3) -> Tuple[str, int]:
    """Collapse consecutive runs of same-fingerprint lines.

    Returns ``(new_text, lines_removed)``.
    """
    lines = text.split("\n")
    out = []
    removed = 0
    i = 0
    n = len(lines)
    while i < n:
        fp = fingerprint(lines[i])
        j = i + 1
        while j < n and fingerprint(lines[j]) == fp and fp != "":
            j += 1
        run = j - i
        if run >= threshold and fp != "":
            out.append(lines[i])
            out.append(f"    … ⟨×{run} similar lines⟩")
            removed += run - 2
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out), max(0, removed)


def cascade_guard(text: str, max_occurrences: int = 5) -> Tuple[str, int]:
    """Prevent error cascades: if one fingerprint dominates the whole output,
    keep the first ``max_occurrences`` and drop the rest, noting the total.

    Unlike :func:`collapse_repeats` this catches *non-consecutive* repetition —
    the same exception re-thrown throughout a long trace.
    """
    lines = text.split("\n")
    counts: dict[str, int] = {}
    for ln in lines:
        fp = fingerprint(ln)
        if fp:
            counts[fp] = counts.get(fp, 0) + 1
    dominant = {fp for fp, c in counts.items() if c > max_occurrences}
    if not dominant:
        return text, 0
    seen: dict[str, int] = {}
    out = []
    removed = 0
    for ln in lines:
        fp = fingerprint(ln)
        if fp in dominant:
            seen[fp] = seen.get(fp, 0) + 1
            if seen[fp] <= max_occurrences:
                out.append(ln)
            elif seen[fp] == max_occurrences + 1:
                out.append(f"    … ⟨and {counts[fp] - max_occurrences} more like this⟩")
                removed += 1
            else:
                removed += 1
        else:
            out.append(ln)
    return "\n".join(out), removed


# Search results group by file, and the grouping is the whole point: a reader
# wants to know which files matched and what a match looks like, not to read
# the two hundredth hit in the same file. The two largest outputs winnow has
# ever stored were both ripgrep runs, at 76 and 25 million tokens, and neither
# collapse_repeats nor cascade_guard touches them -- the hits differ in line
# number and in content, so nothing looks repeated to a fingerprint.
_GREP_HIT = re.compile(r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):")


def limit_per_file(text: str, keep: int = 5) -> tuple[str, int]:
    """Cap how many hits from the same file survive, keeping the first `keep`.

    Only lines in ripgrep's `path:line:` form are grouped. Anything else --
    headers, binary-file notices, error text, a summary line -- passes through
    untouched, because a rule that eats the one line explaining an empty result
    is worse than no rule.
    """
    if keep < 1:
        return text, 0
    lines = text.split("\n")

    counts: dict[str, int] = {}
    for ln in lines:
        m = _GREP_HIT.match(ln)
        if m:
            f = m.group("file")
            counts[f] = counts.get(f, 0) + 1
    if not any(c > keep for c in counts.values()):
        return text, 0

    seen: dict[str, int] = {}
    out = []
    removed = 0
    for ln in lines:
        m = _GREP_HIT.match(ln)
        if not m:
            out.append(ln)
            continue
        f = m.group("file")
        seen[f] = seen.get(f, 0) + 1
        if seen[f] <= keep:
            out.append(ln)
        elif seen[f] == keep + 1:
            out.append(f"    … ⟨+{counts[f] - keep} more in {f}⟩")
            removed += 1
        else:
            removed += 1
    return "\n".join(out), removed
