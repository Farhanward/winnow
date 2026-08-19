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


def efficiency_path() -> Path:
    return home() / "efficiency.db"


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
    # Cap the recall store by size as well as by row count. A row cap alone
    # never fires on the outputs that actually fill the disk: one `rg` sweep
    # can store hundreds of megabytes in a single row, so 5000 rows can mean
    # a gigabyte on disk while the count sits at 127. Oldest rows are dropped
    # until the stored payload fits, then the file is vacuumed so the space
    # comes back. 0 turns the size cap off and leaves the row cap alone.
    max_store_bytes: int = 268_435_456  # 256 MiB
    # Cap one stored payload. Recall exists so a compressed view is never the
    # only copy, and the head of a huge output is what a reader actually goes
    # back for. Anything past this is dropped with a marker line recording how
    # much went, rather than being kept in full. 0 stores every byte.
    max_row_bytes: int = 4_194_304  # 4 MiB

    # --- input clamping (the PreToolUse hook on Read and Grep) ------------- #
    # These caps do not compress anything. They cap what the agent asks for,
    # which is the only lever a PreToolUse hook has over a tool whose output
    # never leaves the agent. Set any of them to 0 to turn that clamp off.
    #
    # Grep, content mode: a row is a whole matched source line plus a
    # ``path:line:`` prefix, and -A/-B/-C multiply it, so a content row costs
    # several times what a path row costs. The tool's own default is 250 rows;
    # 80 is enough to read the shape of the matches and pick the next query.
    grep_head_limit_content: int = 80
    # Grep, files_with_matches and count: one short path (or one count) per
    # row, so rows are cheap and the cap can be loose. Still under the tool's
    # 250, because a pattern with more than 200 matching files is a pattern to
    # narrow rather than a list to read.
    grep_head_limit_paths: int = 200
    # Read: the tool already stops at 2000 lines, so a clamp only earns its
    # keep on files longer than that. At roughly 60 bytes a line, 2000 lines is
    # about 120 KB, so 128 KiB is a cheap size proxy for "longer than the tool
    # would return anyway". Smaller files are left alone.
    read_large_file_bytes: int = 131_072
    # Lines to request from a file over that threshold. One screenful of
    # orientation; a follow-up Read with an explicit offset gets the rest.
    read_clamp_lines: int = 400

    # --- repeated reads --------------------------------------------------- #
    # 39% of the Read calls measured on the development machine asked for a
    # file the same session had already been given. When the file has not
    # changed since, the second copy is spend on content the model already
    # holds, so the request is cut to a stub and the model is told why. False
    # turns this off and every read is served in full.
    dedupe_reads: bool = True
    # How long a served read stays in the ledger. A session running longer
    # than this has almost certainly compacted its context, so what the model
    # can still see is no longer what the ledger recorded. 0 never forgets,
    # which is only sensible with compaction hooked up.
    dedupe_window_seconds: int = 7200

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
