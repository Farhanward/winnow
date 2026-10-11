"""Build compact, reference-based prompts for fresh workers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Sequence

from . import tokens


def _path_key(path: Path) -> str:
    return str(path).casefold() if os.name == "nt" else str(path)


def _paths(values: Iterable[str], *, must_exist: bool) -> List[Path]:
    result: List[Path] = []
    seen = set()
    for value in values:
        path = Path(value).expanduser().resolve(strict=False)
        if must_exist and not path.is_file():
            raise ValueError(f"path does not exist or is not a file: {value}")
        key = _path_key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def pack(
    references: Sequence[str],
    *,
    task: str,
    owns: Sequence[str] = (),
    checks: Sequence[str] = (),
    report_words: int = 120,
) -> dict:
    """Return a compact worker prompt and token accounting."""
    try:
        report_words = int(report_words)
    except (TypeError, ValueError) as exc:
        raise ValueError("--report-words must be an integer") from exc
    if not task.strip():
        raise ValueError("--task must not be empty")
    if report_words < 0:
        raise ValueError("--report-words must be non-negative")

    reference_paths = _paths(references, must_exist=True)
    # Owned paths may be new files the worker is expected to create.
    owned_paths = _paths(owns, must_exist=False)
    check_values = list(dict.fromkeys(str(check) for check in checks))

    lines = [
        "Start fresh with no inherited conversation.",
        "Use file paths as references; never embed or repeat their contents.",
        "Read these exact reference files before acting:",
        *[f"- {path}" for path in reference_paths],
    ]
    if owned_paths:
        lines += ["Edit these owned paths directly:", *[f"- {path}" for path in owned_paths]]
    if check_values:
        lines += ["Acceptance checks:", *[f"- {check}" for check in check_values]]
    lines += [f"Task: {task.strip()}"]
    if report_words:
        lines.append(f"Return a report of no more than {report_words} words.")
    prompt = "\n".join(lines)

    referenced_tokens = sum(
        tokens.estimate(path.read_text(encoding="utf-8", errors="replace"))
        for path in reference_paths
    )
    embedded_tokens = tokens.estimate(prompt)
    return {
        "prompt": prompt,
        "references": [str(path) for path in reference_paths],
        "owns": [str(path) for path in owned_paths],
        "checks": check_values,
        "report_words": report_words,
        "embedded_tokens": embedded_tokens,
        "referenced_tokens": referenced_tokens,
        "avoided_tokens": max(referenced_tokens - embedded_tokens, 0),
    }
