"""Configuration and paths for Winnow.

Everything Winnow persists lives under a single home directory (``WINNOW_HOME``,
default ``~/.winnow``): the recall store and the user's custom rule packs. A
small ``config.json`` in that directory can override thresholds.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


def home() -> Path:
    """Return the Winnow home directory, creating it on first use."""
    root = os.environ.get("WINNOW_HOME")
    path = Path(root).expanduser() if root else Path.home() / ".winnow"
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_path() -> Path:
    return home() / "store.db"


def user_rules_dir() -> Path:
    d = home() / "rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    # Minimum fraction of tokens we must save for compression to be kept.
    # Below this we pass the original through untouched (safety-first).
    min_saving: float = 0.15
    # Outputs below this many tokens are never compressed — not worth it.
    min_tokens: int = 40
    # When collapsing repeated log lines, collapse runs of at least this size.
    collapse_threshold: int = 3
    # For head/tail style trims, how many lines to keep by default.
    keep_head: int = 40
    keep_tail: int = 20
    # Cap the recall store at this many rows (oldest pruned first).
    max_store_rows: int = 5000

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        f = home() / "config.json"
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (json.JSONDecodeError, OSError):
                pass
        return cfg

    def save(self) -> None:
        (home() / "config.json").write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )
