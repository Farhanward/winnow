"""The recall store — Winnow's safety net.

Every command Winnow wraps has its *full* output teed into a local SQLite
database before any compression happens. That guarantees compression is never
lossy in practice: whatever we trim from the model-facing view is still on disk,
addressable by a short handle (e.g. ``a1b2c3``) and searchable full-text.

When SQLite is built with FTS5 (the common case) we get real full-text recall;
otherwise we transparently fall back to ``LIKE`` scans so the feature still
works everywhere.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

from . import config

HANDLE_RE = re.compile(r"^[0-9a-f]{6}$")


@dataclass
class Record:
    id: str
    ts: float
    command: str
    cwd: str
    exit_code: int
    raw: str
    raw_tokens: int
    comp_tokens: int
    filt: str


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = str(path) if path else str(config.store_path())
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.has_fts = self._init_schema()

    def _init_schema(self) -> bool:
        c = self.conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS outputs (
                id          TEXT PRIMARY KEY,
                ts          REAL,
                command     TEXT,
                cwd         TEXT,
                exit_code   INTEGER,
                raw         TEXT,
                raw_tokens  INTEGER,
                comp_tokens INTEGER,
                filt        TEXT
            )
            """
        )
        has_fts = False
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS outputs_fts "
                "USING fts5(id UNINDEXED, command, raw)"
            )
            has_fts = True
        except sqlite3.OperationalError:
            has_fts = False  # FTS5 not compiled in — LIKE fallback used instead
        c.commit()
        return has_fts

    def _new_handle(self) -> str:
        for _ in range(20):
            h = secrets.token_hex(3)  # 6 hex chars, human-friendly
            row = self.conn.execute(
                "SELECT 1 FROM outputs WHERE id = ?", (h,)
            ).fetchone()
            if row is None:
                return h
        return secrets.token_hex(4)

    def put(
        self,
        command: str,
        cwd: str,
        exit_code: int,
        raw: str,
        raw_tokens: int,
        comp_tokens: int,
        filt: str,
    ) -> str:
        """Store a full raw output and return its recall handle."""
        h = self._new_handle()
        ts = time.time()
        self.conn.execute(
            "INSERT INTO outputs VALUES (?,?,?,?,?,?,?,?,?)",
            (h, ts, command, cwd, exit_code, raw, raw_tokens, comp_tokens, filt),
        )
        if self.has_fts:
            self.conn.execute(
                "INSERT INTO outputs_fts (id, command, raw) VALUES (?,?,?)",
                (h, command, raw),
            )
        self.conn.commit()
        self._prune()
        return h

    def _prune(self) -> None:
        cfg = config.Config.load()
        n = self.conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
        if n <= cfg.max_store_rows:
            return
        excess = n - cfg.max_store_rows
        old = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM outputs ORDER BY ts ASC LIMIT ?", (excess,)
            ).fetchall()
        ]
        if not old:
            return
        marks = ",".join("?" * len(old))
        self.conn.execute(f"DELETE FROM outputs WHERE id IN ({marks})", old)
        if self.has_fts:
            self.conn.execute(f"DELETE FROM outputs_fts WHERE id IN ({marks})", old)
        self.conn.commit()

    def get(self, handle: str) -> Optional[Record]:
        row = self.conn.execute(
            "SELECT * FROM outputs WHERE id = ?", (handle,)
        ).fetchone()
        return self._row(row) if row else None

    def search(self, query: str, limit: int = 10) -> List[Record]:
        rows = []
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT o.* FROM outputs_fts f JOIN outputs o ON o.id = f.id "
                    "WHERE outputs_fts MATCH ? ORDER BY o.ts DESC LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:  # fallback / no-FTS path
            like = f"%{query}%"
            rows = self.conn.execute(
                "SELECT * FROM outputs WHERE command LIKE ? OR raw LIKE ? "
                "ORDER BY ts DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def recent(self, limit: int = 20) -> List[Record]:
        rows = self.conn.execute(
            "SELECT * FROM outputs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def totals(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) n, "
            "COALESCE(SUM(raw_tokens),0) raw, "
            "COALESCE(SUM(comp_tokens),0) comp FROM outputs"
        ).fetchone()
        raw, comp = row["raw"], row["comp"]
        saved = raw - comp
        pct = (saved / raw * 100) if raw else 0.0
        return {"count": row["n"], "raw": raw, "comp": comp, "saved": saved, "pct": pct}

    @staticmethod
    def _row(row: sqlite3.Row) -> Record:
        return Record(
            id=row["id"],
            ts=row["ts"],
            command=row["command"],
            cwd=row["cwd"],
            exit_code=row["exit_code"],
            raw=row["raw"],
            raw_tokens=row["raw_tokens"],
            comp_tokens=row["comp_tokens"],
            filt=row["filt"],
        )

    def close(self) -> None:
        self.conn.close()


def is_handle(s: str) -> bool:
    return bool(HANDLE_RE.match(s))
