"""The savings ledger - a lifetime total that pruning cannot reach.

`wn gain` reads the recall store, which is a cache with a size cap on it. That
made the published figure decay: this machine reported 127 outputs and 84.4%
while the store still held two `rg` sweeps worth 102 million tokens between
them, and 90 outputs and 68.3% once the size cap pruned those rows. Neither
number was miscounted. The command describes what the store holds, and pruning
changes what the store holds.

A lifetime figure has to live somewhere pruning does not go, so it lives here:
integer counters in `~/.winnow/savings.db`, written once per compressed output
and never rewritten.

Two things this counts that `wn gain` never did, both because leaving them out
flatters the result:

**Passthrough.** Every output Winnow looked at and handed back unchanged is
counted. Reporting a reduction over only the outputs that compressed picks the
denominator after seeing the answer. The honest denominator is everything the
compressor was given, and the ratio against it is always the smaller number.

**The tail.** Savings on this machine were dominated by a handful of enormous
outputs, and an average hides that completely. The largest single saving is
kept alongside the total so the shape stays visible: a total that rests on one
output is a different claim from the same total spread over a thousand.

Filter labels are recorded, since they are Winnow's own vocabulary. Commands,
paths, and output never are.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Dict, List, Optional

from . import config


def ledger_path():
    return config.home() / "savings.db"


class Savings:
    def __init__(self, path: Optional[str] = None):
        self.conn = sqlite3.connect(str(path or ledger_path()))
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lifetime (
                id                   INTEGER PRIMARY KEY CHECK (id = 1),
                outputs_seen         INTEGER DEFAULT 0,
                outputs_compressed   INTEGER DEFAULT 0,
                raw_tokens           INTEGER DEFAULT 0,
                out_tokens           INTEGER DEFAULT 0,
                passthrough_tokens   INTEGER DEFAULT 0,
                largest_saving       INTEGER DEFAULT 0,
                first_seen           REAL,
                last_seen            REAL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS by_filter (
                label      TEXT PRIMARY KEY,
                outputs    INTEGER DEFAULT 0,
                raw_tokens INTEGER DEFAULT 0,
                out_tokens INTEGER DEFAULT 0
            )
            """
        )
        self.conn.execute("INSERT OR IGNORE INTO lifetime (id) VALUES (1)")
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def record(
        self,
        raw_tokens: int,
        comp_tokens: int,
        label: str,
        passthrough: bool,
    ) -> None:
        """Add one output to the lifetime totals.

        A passthrough moves the denominator and nothing else. Counting its
        tokens as compressed would report a saving Winnow did not make; not
        counting the output at all would report a rate over a denominator
        chosen after the fact.
        """
        now = time.time()
        if passthrough:
            self.conn.execute(
                "UPDATE lifetime SET outputs_seen = outputs_seen + 1, "
                "passthrough_tokens = passthrough_tokens + ?, "
                "first_seen = COALESCE(first_seen, ?), last_seen = ? "
                "WHERE id = 1",
                (int(raw_tokens), now, now),
            )
            self.conn.commit()
            return

        saved = max(0, int(raw_tokens) - int(comp_tokens))
        self.conn.execute(
            "UPDATE lifetime SET outputs_seen = outputs_seen + 1, "
            "outputs_compressed = outputs_compressed + 1, "
            "raw_tokens = raw_tokens + ?, out_tokens = out_tokens + ?, "
            "largest_saving = MAX(largest_saving, ?), "
            "first_seen = COALESCE(first_seen, ?), last_seen = ? "
            "WHERE id = 1",
            (int(raw_tokens), int(comp_tokens), saved, now, now),
        )
        self.conn.execute(
            "INSERT INTO by_filter (label, outputs, raw_tokens, out_tokens) "
            "VALUES (?,1,?,?) ON CONFLICT(label) DO UPDATE SET "
            "outputs = outputs + 1, raw_tokens = raw_tokens + excluded.raw_tokens, "
            "out_tokens = out_tokens + excluded.out_tokens",
            (str(label or "none"), int(raw_tokens), int(comp_tokens)),
        )
        self.conn.commit()

    def totals(self) -> Dict[str, float]:
        row = self.conn.execute(
            "SELECT outputs_seen, outputs_compressed, raw_tokens, out_tokens, "
            "passthrough_tokens, largest_saving, first_seen, last_seen "
            "FROM lifetime WHERE id = 1"
        ).fetchone()
        if row is None:
            row = (0, 0, 0, 0, 0, 0, None, None)
        seen, compressed, raw, out, through, largest, first, last = row
        saved = max(0, (raw or 0) - (out or 0))
        # Two rates, and the second is the one that cannot be gamed. The first
        # divides by the outputs that compressed; the second divides by
        # everything the compressor was handed.
        on_compressed = (saved / raw * 100) if raw else 0.0
        overall_in = (raw or 0) + (through or 0)
        overall = (saved / overall_in * 100) if overall_in else 0.0
        return {
            "outputs_seen": seen or 0,
            "outputs_compressed": compressed or 0,
            "outputs_passthrough": (seen or 0) - (compressed or 0),
            "raw_tokens": raw or 0,
            "out_tokens": out or 0,
            "passthrough_tokens": through or 0,
            "tokens_saved": saved,
            "reduction_on_compressed_pct": round(on_compressed, 1),
            "reduction_overall_pct": round(overall, 1),
            "largest_single_saving": largest or 0,
            "largest_share_pct": round((largest or 0) * 100 / saved, 1) if saved else 0.0,
            "first_seen": first,
            "last_seen": last,
        }

    def by_filter(self, limit: int = 10) -> List[Dict[str, int]]:
        rows = self.conn.execute(
            "SELECT label, outputs, raw_tokens, out_tokens FROM by_filter "
            "ORDER BY (raw_tokens - out_tokens) DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "label": label,
                "outputs": outputs,
                "tokens_saved": max(0, (raw or 0) - (out or 0)),
                "reduction_pct": round(
                    ((raw - out) / raw * 100) if raw else 0.0, 1
                ),
            }
            for label, outputs, raw, out in rows
        ]

    def reset(self) -> None:
        self.conn.execute("DELETE FROM by_filter")
        self.conn.execute(
            "UPDATE lifetime SET outputs_seen = 0, outputs_compressed = 0, "
            "raw_tokens = 0, out_tokens = 0, passthrough_tokens = 0, "
            "largest_saving = 0, first_seen = NULL, last_seen = NULL WHERE id = 1"
        )
        self.conn.commit()


def record(raw_tokens: int, comp_tokens: int, label: str, passthrough: bool) -> None:
    """Record one output, and never let the bookkeeping break the compression.

    This sits on the path of every wrapped command. A ledger that cannot be
    opened costs a counter; an exception here would cost the output.
    """
    ledger = None
    try:
        ledger = Savings()
        ledger.record(raw_tokens, comp_tokens, label, passthrough)
    except sqlite3.Error:
        pass
    finally:
        if ledger is not None:
            ledger.close()
