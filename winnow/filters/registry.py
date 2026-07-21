"""Registry mapping commands to built-in filter functions."""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

# A filter takes (raw_output, config) and returns compressed text, or None to
# decline (leaving the output for the next layer to handle).
Filter = Callable[[str, object], Optional[str]]

_FILTERS: List[Tuple[re.Pattern, Filter, str]] = []


def register(pattern: str, name: str):
    """Decorator: register ``func`` for commands matching ``pattern`` (regex)."""
    compiled = re.compile(pattern, re.IGNORECASE)

    def deco(func: Filter) -> Filter:
        _FILTERS.append((compiled, func, name))
        return func

    return deco


def detect(command: str) -> Tuple[Optional[Filter], Optional[str]]:
    """Return the first (filter, name) whose pattern matches ``command``."""
    cmd = command.strip()
    for pattern, func, name in _FILTERS:
        if pattern.search(cmd):
            return func, name
    return None, None


def all_filters() -> List[str]:
    return [name for _, _, name in _FILTERS]
