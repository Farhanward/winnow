"""Tests for the recall store's size caps.

The row cap alone never fires on the outputs that fill a disk. One `rg` sweep
over a large tree is a single row holding hundreds of megabytes, so a store can
sit well inside a 5000-row cap and still be a gigabyte on disk. These cover the
byte cap and the per-output cap that close that gap.
"""

import json

import pytest

from winnow import config
from winnow.store import Store, _capped


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNOW_HOME", str(tmp_path / "wh"))
    return tmp_path


def _write_config(**values):
    (config.home() / "config.json").write_text(
        json.dumps(values), encoding="utf-8"
    )


def _put(store, text, command="rg -n pattern"):
    return store.put(command, "/tmp", 0, text, len(text) // 4, 10, "ripgrep")


def test_oversized_payload_is_stored_head_only(isolated_home):
    _write_config(max_row_bytes=1024)
    store = Store()
    handle = _put(store, "x" * 5000)
    record = store.get(handle)
    store.close()

    assert len(record.raw.encode("utf-8")) < 1400
    assert "stored head only" in record.raw
    assert "3,976 more bytes dropped" in record.raw


def test_token_counts_are_not_rewritten_by_the_row_cap(isolated_home):
    """A capped payload must not quietly deflate the published savings.

    `wn gain` reports what the output cost the agent, which is a property of
    the output, not of how much of it we chose to keep on disk.
    """
    _write_config(max_row_bytes=512)
    store = Store()
    handle = store.put("rg -n x", "/tmp", 0, "y" * 40_000, 10_000, 120, "ripgrep")
    record = store.get(handle)
    store.close()

    assert record.raw_tokens == 10_000
    assert record.comp_tokens == 120


def test_byte_cap_drops_oldest_rows_until_the_payload_fits(isolated_home):
    _write_config(max_store_bytes=6000, max_row_bytes=0)
    store = Store()
    for _ in range(6):
        _put(store, "z" * 2000)
    remaining = store.conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
    payload = store._payload_bytes()
    store.close()

    assert payload <= 6000
    assert remaining == 3


def test_byte_cap_keeps_the_newest_row(isolated_home):
    _write_config(max_store_bytes=3000, max_row_bytes=0)
    store = Store()
    _put(store, "old" * 500)
    newest = _put(store, "new" * 500)
    rows = [r[0] for r in store.conn.execute("SELECT id FROM outputs").fetchall()]
    store.close()

    assert newest in rows


def test_zero_turns_a_cap_off(isolated_home):
    _write_config(max_store_bytes=0, max_row_bytes=0)
    store = Store()
    for _ in range(4):
        _put(store, "q" * 10_000)
    count = store.conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
    store.close()

    assert count == 4


def test_unreadable_cap_disables_only_itself(isolated_home):
    """A hand-edited config must never fail the command being wrapped.

    README says 0 turns a cap off, which makes null a reasonable guess for a
    reader after the same thing. A value that cannot be read turns off its own
    cap and leaves the rest working.
    """
    _write_config(max_row_bytes=None, max_store_bytes="lots")
    store = Store()
    handle = _put(store, "w" * 3000)
    record = store.get(handle)
    store.close()

    assert len(record.raw) == 3000


def test_row_cap_still_applies_alongside_the_byte_cap(isolated_home):
    _write_config(max_store_rows=2, max_store_bytes=0, max_row_bytes=0)
    store = Store()
    for _ in range(5):
        _put(store, "short output")
    count = store.conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
    store.close()

    assert count == 2


def test_capped_helper_leaves_small_payloads_untouched():
    cfg = config.Config()
    assert _capped("small", cfg) == "small"


def test_capped_helper_cuts_on_a_character_boundary():
    """A cut inside a multi-byte character must not produce mojibake."""
    cfg = config.Config()
    cfg.max_row_bytes = 5
    # Three-byte characters, so a naive 5-byte slice lands mid-character.
    out = _capped("中文中文", cfg)

    assert out.startswith("中")
    assert "stored head only" in out
    out.encode("utf-8")  # round-trips without raising
