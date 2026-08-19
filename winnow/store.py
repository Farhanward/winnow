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


def _capped(raw: str, cfg) -> str:
    """Cut one payload down to the per-row cap before it is stored.

    Recall exists so that a compressed view is never the only copy of an
    output, and what a reader goes back for is the head of it. Keeping a
    300 MB `rg` sweep in full serves nobody and is most of how a store reaches
    a gigabyte. The cut is recorded in the text, so recall never quietly
    returns a short answer to a long question.
    """
    try:
        cap = int(cfg.max_row_bytes)
    except (TypeError, ValueError):
        return raw
    if cap <= 0:
        return raw
    data = raw.encode("utf-8", "replace")
    if len(data) <= cap:
        return raw
    kept = data[:cap].decode("utf-8", "ignore")
    dropped = len(data) - len(kept.encode("utf-8"))
    marker = (
        f"\n[winnow: stored head only, {dropped:,} more bytes dropped "
        f"at the {cap:,}-byte per-output cap]\n"
    )
    return kept + marker


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
        raw = _capped(raw, config.Config.load())
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
        dropped = self._prune_rows(cfg) + self._prune_bytes(cfg)
        if dropped:
            self.conn.commit()
            self._reclaim(cfg)

    def _prune_rows(self, cfg) -> int:
        """Drop the oldest rows until the row count fits."""
        try:
            cap = int(cfg.max_store_rows)
        except (TypeError, ValueError):
            return 0
        if cap <= 0:
            return 0
        n = self.conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
        if n <= cap:
            return 0
        return self._delete_oldest(n - cap)

    def _prune_bytes(self, cfg) -> int:
        """Drop the oldest rows until the stored payload fits the size cap.

        The row cap never fires on the outputs that fill a disk. One `rg`
        sweep over a large tree is a single row holding hundreds of megabytes,
        so a store can pass a 5000-row cap with 127 rows in it and a gigabyte
        on disk. This counts bytes instead, oldest first, and stops as soon as
        the total is under the cap.
        """
        try:
            cap = int(cfg.max_store_bytes)
        except (TypeError, ValueError):
            return 0
        if cap <= 0:
            return 0
        total = self._payload_bytes()
        if total <= cap:
            return 0
        dropped = 0
        # Oldest first, one row at a time: rows differ in size by orders of
        # magnitude, and deleting a batch sized from the average would throw
        # away far more history than the cap asks for.
        for rid, size in self.conn.execute(
            "SELECT id, LENGTH(CAST(raw AS BLOB)) FROM outputs ORDER BY ts ASC"
        ).fetchall():
            if total <= cap:
                break
            self._delete_ids([rid])
            total -= size or 0
            dropped += 1
        return dropped

    def _payload_bytes(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(raw AS BLOB))), 0) FROM outputs"
        ).fetchone()
        return int(row[0] or 0)

    def _delete_oldest(self, count: int) -> int:
        old = [
            r[0]
            for r in self.conn.execute(
                "SELECT id FROM outputs ORDER BY ts ASC LIMIT ?", (count,)
            ).fetchall()
        ]
        self._delete_ids(old)
        return len(old)

    def _delete_ids(self, ids) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM outputs WHERE id IN ({marks})", list(ids))
        if self.has_fts:
            self.conn.execute(
                f"DELETE FROM outputs_fts WHERE id IN ({marks})", list(ids)
            )

    def _reclaim(self, cfg) -> None:
        """Give the freed pages back to the filesystem.

        SQLite keeps deleted pages for reuse, so a store that has just dropped
        900 MB still reports 900 MB to `du` until it is vacuumed. VACUUM
        rewrites the whole file, which is slow enough that doing it on every
        prune would be felt on the command being wrapped, so it runs only when
        the free space is worth the rewrite.
        """
        try:
            page = self.conn.execute("PRAGMA page_size").fetchone()[0]
            free = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
            total = self.conn.execute("PRAGMA page_count").fetchone()[0]
        except sqlite3.Error:
            return
        free_bytes = int(page) * int(free)
        file_bytes = int(page) * int(total)
        if free_bytes < 33_554_432 and free_bytes < file_bytes // 10:
            return
        try:
            self.conn.execute("VACUUM")
        except sqlite3.Error:
            # A vacuum can fail on a locked or read-only file. Nothing is lost
            # by skipping it: the rows are already gone and the next prune
            # will try again.
            pass

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
