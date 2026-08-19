"""The read ledger - remembering what the agent has already been shown.

Winnow's first half compresses what a command *returns*. This is the other
direction: what the agent *asks for*, and specifically what it asks for twice.

The measurement that produced this file: across the Claude Code transcripts on
the development machine, 958 `Read` calls resolved to 581 distinct file and
session pairs. 377 of them, 39% of every read, were the same file read again
inside the same session. The worst single file was read sixteen times. A
re-read costs the full file again, and when the file has not changed in
between, all of it is a second copy of something the agent already has.

The ledger records what was served: session, path, byte range, and a cheap
fingerprint of the file at the time. When the same range of an unchanged file
is requested again, the hook caps that call to a stub and tells the model where
the content it already has came from.

Two design choices are load-bearing:

**A stub, not a denial.** A `PreToolUse` deny shows its reason to the user and
not to the model, so a blocked read would leave the agent with a failed tool
call and no explanation. Capping the request to a single line goes through the
same `updatedInput` path every other clamp uses, saves nearly as much, and lets
`additionalContext` explain what happened.

**Ask twice and it goes through.** Context gets compacted, and the earlier read
may no longer be in the window the model can see. Insisting is therefore always
honoured: a suppressed read is marked, and the next identical request is served
in full. The cost of being wrong is one extra round trip, against re-reading a
file that a model already has in front of it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from . import config

# A record older than this is forgotten. A session that has run long enough for
# a file to age out has almost certainly compacted its context by then, so the
# model may no longer hold what the ledger thinks it holds.
DEFAULT_WINDOW_SECONDS = 7200

# Below this, two records are the same tool call rather than a repeat. No model
# asks for the same file twice inside a second; a settings file with two winnow
# hook entries does exactly that.
SAME_CALL_SECONDS = 1.0


@dataclass
class Seen:
    """A read that was already served."""

    served_at: float
    suppressed: bool


def ledger_path():
    return config.home() / "reads.db"


class Ledger:
    def __init__(self, path: Optional[str] = None):
        self.conn = sqlite3.connect(str(path or ledger_path()))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reads (
                session   TEXT,
                key       TEXT,
                stamp     TEXT,
                served_at REAL,
                suppressed INTEGER DEFAULT 0,
                PRIMARY KEY (session, key)
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # -- keys ------------------------------------------------------------- #
    @staticmethod
    def key(path: str, offset, limit) -> str:
        """Identify one requested range of one file.

        Offset and limit are part of the identity. Reading lines 1-400 and then
        lines 400-800 asks for different content, and only an identical range
        can be answered with what the model already has.
        """
        return f"{os.path.normcase(os.path.abspath(path))}|{offset or 1}|{limit or 0}"

    @staticmethod
    def stamp(path: str) -> Optional[str]:
        """A cheap fingerprint of the file as it is right now.

        Size and modification time, not a content hash. This runs on every
        `Read` the agent makes, so hashing a large file on the hot path would
        cost more than the re-read it is trying to save. The failure mode of
        the cheap check is a file edited within the same nanosecond stamp and
        to exactly the same length, which no editor produces in practice.
        """
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return None
        return f"{st.st_size}:{st.st_mtime_ns}"

    # -- the decision ------------------------------------------------------ #
    def check(self, session: str, path: str, offset, limit, window: int) -> Optional[Seen]:
        """Return the earlier read if this one repeats it, else None.

        None means serve the read normally: it is new, the file changed, the
        record aged out, or this request already had one suppressed attempt.
        """
        stamp = self.stamp(path)
        if stamp is None:
            return None
        row = self.conn.execute(
            "SELECT served_at, suppressed, stamp FROM reads WHERE session = ? AND key = ?",
            (session, self.key(path, offset, limit)),
        ).fetchone()
        if row is None:
            return None
        served_at, suppressed, old_stamp = row
        if old_stamp != stamp:
            # The file changed. What the model holds is stale, so this is a
            # genuinely new read rather than a repeat.
            return None
        if window and time.time() - float(served_at or 0) > window:
            return None
        if time.time() - float(served_at or 0) < SAME_CALL_SECONDS:
            # Too soon to be a re-read. A settings file carrying two winnow
            # entries fires the hook twice for one tool call, and without this
            # the second pass would suppress the first read of every file. The
            # installer removes duplicates now; this makes a hand-edited one
            # harmless rather than harmful.
            return None
        if suppressed:
            # Asked twice. The model is insisting, and it may well be right:
            # its context could have been compacted since. Serve it.
            return None
        return Seen(served_at=float(served_at or 0), suppressed=False)

    def record(self, session: str, path: str, offset, limit, suppressed: bool) -> None:
        stamp = self.stamp(path)
        if stamp is None:
            return
        key = self.key(path, offset, limit)
        now = time.time()
        if suppressed:
            # Mark the suppression against the existing record, keeping the
            # original served_at so the note can say when the content was sent.
            self.conn.execute(
                "UPDATE reads SET suppressed = 1 WHERE session = ? AND key = ?",
                (session, key),
            )
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO reads (session, key, stamp, served_at, suppressed) "
                "VALUES (?,?,?,?,0)",
                (session, key, stamp, now),
            )
        self.conn.commit()

    def forget_session(self, session: str) -> int:
        """Drop everything remembered for one session.

        Called when context is compacted: the window the model can see has just
        been rewritten, so nothing the ledger believes it holds can be trusted.
        """
        cur = self.conn.execute("DELETE FROM reads WHERE session = ?", (session,))
        self.conn.commit()
        return cur.rowcount

    def prune(self, window: int) -> int:
        if not window:
            return 0
        cur = self.conn.execute(
            "DELETE FROM reads WHERE served_at < ?", (time.time() - window * 4,)
        )
        self.conn.commit()
        return cur.rowcount


def elapsed(seconds: float) -> str:
    """Say how long ago something happened, in words a sentence can use."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "moments ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'} ago"
