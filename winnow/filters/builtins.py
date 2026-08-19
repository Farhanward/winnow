"""Built-in structural filters for common noisy commands."""

from __future__ import annotations

import re
from typing import List, Optional

from .. import patterns
from .registry import register


def _cap_lines(lines: List[str], head: int, tail: int, label: str) -> List[str]:
    """Keep the first ``head`` and last ``tail`` lines, note what was hidden."""
    kept, _ = patterns.keep_ends(lines, head, tail, label)
    return kept


@register(r"\bgit\s+status\b", "git-status")
def git_status(raw: str, cfg) -> Optional[str]:
    """Drop git's parenthetical help hints and cap long file lists."""
    lines = raw.split("\n")
    kept, dropped = [], 0
    for ln in lines:
        # Hint lines like: (use "git restore --staged <file>..." to unstage)
        if re.match(r'^\s*\(use "?git', ln) or re.match(r"^\s*\(use ", ln):
            dropped += 1
            continue
        kept.append(ln)
    # Collapse runs of 3+ blank lines that the hint removal may have created.
    out: List[str] = []
    blanks = 0
    for ln in kept:
        if ln.strip() == "":
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(ln)
    out = _cap_lines(out, cfg.keep_head, cfg.keep_tail, "status lines")
    if dropped == 0 and out == lines:
        return None
    return "\n".join(out)


_PIP_NOISE = re.compile(
    r"^(Requirement already satisfied|"
    r"\s*(Downloading|Using cached|Collecting|Obtaining|Getting|Preparing metadata"
    r"|Building wheel|Created wheel|Stored in directory|WARNING: )).*",
    re.IGNORECASE,
)


@register(r"\bpip\d?\s+install\b|\bpython\s+-m\s+pip\s+install\b", "pip-install")
def pip_install(raw: str, cfg) -> Optional[str]:
    """Hide pip's download/collect chatter, keep the outcome and any errors."""
    kept, dropped = [], 0
    for ln in raw.split("\n"):
        if _PIP_NOISE.match(ln) and "error" not in ln.lower():
            dropped += 1
            continue
        kept.append(ln)
    if dropped == 0:
        return None
    kept.append(f"… ⟨{dropped} pip progress lines hidden⟩")
    return "\n".join(kept)


@register(r"\bnpm\s+(install|ci|i)\b|\byarn\s+(install|add)\b|\bpnpm\s+(install|i|add)\b",
          "npm-install")
def npm_install(raw: str, cfg) -> Optional[str]:
    """Drop npm/yarn warn+notice spam, keep the added/removed/audit summary."""
    kept, dropped = [], 0
    for ln in raw.split("\n"):
        low = ln.lower().strip()
        if low.startswith(("npm warn", "npm notice", "warning ", "warning:")):
            dropped += 1
            continue
        kept.append(ln)
    if dropped == 0:
        return None
    kept.append(f"… ⟨{dropped} npm warn/notice lines hidden⟩")
    return "\n".join(kept)


_PYTEST_PROGRESS = re.compile(r"^[\s.sFExXpP]+\[\s*\d+%\]\s*$")


@register(r"\bpytest\b|\bpy\.test\b|\bpython\s+-m\s+pytest\b", "pytest")
def pytest_run(raw: str, cfg) -> Optional[str]:
    """Drop the dotted progress lines; keep failures, errors and the summary."""
    kept, dropped = [], 0
    for ln in raw.split("\n"):
        if _PYTEST_PROGRESS.match(ln):
            dropped += 1
            continue
        kept.append(ln)
    if dropped == 0:
        return None
    return "\n".join(kept)


@register(r"\b(ls|dir|ll)\b", "dir-listing")
def dir_listing(raw: str, cfg) -> Optional[str]:
    """Cap very long directory listings."""
    lines = raw.split("\n")
    if len(lines) <= cfg.keep_head + cfg.keep_tail + 1:
        return None
    return "\n".join(_cap_lines(lines, cfg.keep_head, cfg.keep_tail, "entries"))
