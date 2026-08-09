"""Tiny, aggregate-only efficiency metrics for Winnow runtimes.

The collector deliberately accepts numbers only. It never receives or stores
commands, output, working directories, thread IDs, prompts, or model names.
There is no daemon and no network activity: each event is one SQLite upsert.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

from . import config

CLIENTS = ("codex", "claude", "gemini", "local")
_ALIASES = {
    "codex": "codex",
    "claude": "claude",
    "claude-code": "claude",
    # The runtime is Gemini. Antigravity is the editor it happens to sit in,
    # the way Claude Code and Codex are the runtimes rather than the terminals
    # they run in, so the label names the model and the editor is an alias.
    # Neither has a hook system, so an explicit --client is the only route
    # either of them has to these counters.
    "gemini": "gemini",
    "antigravity": "gemini",
    "gemini-antigravity": "gemini",
    "local": "local",
    "localagent": "local",
    "lmstudio": "local",
    "unknown": "unknown",
}


def metrics_path() -> Path:
    return config.efficiency_path()


def detect_client(
    explicit: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve a stable, low-cardinality runtime label."""
    if explicit:
        return _ALIASES.get(explicit.strip().casefold(), "unknown")

    values = os.environ if env is None else env
    # An editor that sets no recognisable variable of its own can still name
    # itself here. Antigravity is the case this was added for: it ships no
    # marker this code could sniff, and guessing one would have produced a
    # label that silently never matched.
    named = values.get("WINNOW_CLIENT")
    if named:
        return _ALIASES.get(named.strip().casefold(), "unknown")
    if values.get("CODEX_THREAD_ID") or values.get("CODEX_HOME"):
        return "codex"
    if (
        values.get("CLAUDECODE")
        or values.get("CLAUDE_CODE")
        or values.get("CLAUDE_CODE_ENTRYPOINT")
    ):
        return "claude"
    if values.get("LOCALAGENT") or values.get("LOCAL_AGENT"):
        return "local"
    return "unknown"


@dataclass(frozen=True)
class Snapshot:
    client: str
    observed: int = 0
    selected: int = 0
    runs: int = 0
    compressed: int = 0
    passthrough: int = 0
    raw_tokens: int = 0
    output_tokens: int = 0
    process_ns: int = 0
    updated_at: float = 0.0

    @property
    def saved_tokens(self) -> int:
        return self.raw_tokens - self.output_tokens

    @property
    def reduction_pct(self) -> float:
        if not self.raw_tokens:
            return 0.0
        return self.saved_tokens / self.raw_tokens * 100

    @property
    def selection_pct(self) -> float:
        if not self.observed:
            return 0.0
        return self.selected / self.observed * 100

    @property
    def average_process_ms(self) -> float:
        if not self.runs:
            return 0.0
        return self.process_ns / self.runs / 1_000_000


class Collector:
    """Aggregate runtime counters in a one-row-per-runtime SQLite database."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else metrics_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=0.25)
        self.conn.row_factory = sqlite3.Row
        # These counters are expendable telemetry, so avoid a disk flush on
        # every hook while retaining SQLite's transactional file format.
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_efficiency (
                client        TEXT PRIMARY KEY,
                observed      INTEGER NOT NULL DEFAULT 0,
                selected      INTEGER NOT NULL DEFAULT 0,
                runs          INTEGER NOT NULL DEFAULT 0,
                compressed    INTEGER NOT NULL DEFAULT 0,
                passthrough   INTEGER NOT NULL DEFAULT 0,
                raw_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                process_ns    INTEGER NOT NULL DEFAULT 0,
                updated_at    REAL NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.commit()

    def observe(self, client: str, selected: bool) -> None:
        label = detect_client(client)
        if label not in CLIENTS:
            return
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO runtime_efficiency
                (client, observed, selected, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(client) DO UPDATE SET
                observed = observed + 1,
                selected = selected + excluded.selected,
                updated_at = excluded.updated_at
            """,
            (label, int(selected), now),
        )
        self.conn.commit()

    def record_run(
        self,
        client: str,
        raw_tokens: int,
        output_tokens: int,
        compressed: bool,
        process_ns: int,
    ) -> None:
        label = detect_client(client)
        if label not in CLIENTS:
            return
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO runtime_efficiency (
                client, runs, compressed, passthrough, raw_tokens,
                output_tokens, process_ns, updated_at
            )
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client) DO UPDATE SET
                runs = runs + 1,
                compressed = compressed + excluded.compressed,
                passthrough = passthrough + excluded.passthrough,
                raw_tokens = raw_tokens + excluded.raw_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                process_ns = process_ns + excluded.process_ns,
                updated_at = excluded.updated_at
            """,
            (
                label,
                int(compressed),
                int(not compressed),
                max(0, int(raw_tokens)),
                max(0, int(output_tokens)),
                max(0, int(process_ns)),
                now,
            ),
        )
        self.conn.commit()

    def snapshot(self) -> Dict[str, Snapshot]:
        rows = {
            row["client"]: Snapshot(**dict(row))
            for row in self.conn.execute(
                "SELECT * FROM runtime_efficiency ORDER BY client"
            )
        }
        return {client: rows.get(client, Snapshot(client)) for client in CLIENTS}

    def close(self) -> None:
        self.conn.close()


def observe(client: str, selected: bool) -> None:
    """Best-effort hook observation; metrics must never disrupt a tool call."""
    try:
        collector = Collector()
        collector.observe(client, selected)
        collector.close()
    except (OSError, sqlite3.Error):
        pass


def record_run(
    client: str,
    raw_tokens: int,
    output_tokens: int,
    compressed: bool,
    process_ns: int,
) -> None:
    """Best-effort run metric; contains aggregate numbers only."""
    try:
        collector = Collector()
        collector.record_run(
            client, raw_tokens, output_tokens, compressed, process_ns
        )
        collector.close()
    except (OSError, sqlite3.Error):
        pass
