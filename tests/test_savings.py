"""Tests for the lifetime savings counter.

`wn gain` used to derive its figure from the recall store, which is a cache
with a size cap. The published number decayed as the store pruned: 127 outputs
and 84.4% became 90 and 68.3% the moment the size cap reached two large rows.
These cover the counter that pruning cannot reach, and the honest denominator
that came with it.
"""

import json
from argparse import Namespace

import pytest

from winnow import cli, core, savings
from winnow.config import Config
from winnow.store import Store


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINNOW_HOME", str(tmp_path / "wh"))
    return tmp_path


def _ledger():
    return savings.Savings()


def test_a_compressed_output_lands_in_the_counter(isolated_home):
    savings.record(1000, 100, "npm-install", passthrough=False)
    led = _ledger()
    totals = led.totals()
    led.close()

    assert totals["outputs_seen"] == 1
    assert totals["outputs_compressed"] == 1
    assert totals["tokens_saved"] == 900
    assert totals["reduction_on_compressed_pct"] == 90.0


def test_a_passthrough_moves_the_denominator_and_nothing_else(isolated_home):
    """Counting its tokens as compressed would report a saving never made."""
    savings.record(500, 500, "passthrough", passthrough=True)
    led = _ledger()
    totals = led.totals()
    led.close()

    assert totals["outputs_seen"] == 1
    assert totals["outputs_compressed"] == 0
    assert totals["tokens_saved"] == 0
    assert totals["passthrough_tokens"] == 500


def test_the_overall_rate_divides_by_everything_seen(isolated_home):
    """The rate over only what compressed picks its denominator after the fact."""
    savings.record(1000, 0, "json", passthrough=False)
    savings.record(1000, 1000, "passthrough", passthrough=True)
    led = _ledger()
    totals = led.totals()
    led.close()

    assert totals["reduction_on_compressed_pct"] == 100.0
    assert totals["reduction_overall_pct"] == 50.0


def test_the_largest_single_saving_is_kept_visible(isolated_home):
    """A total resting on one output is a different claim from a spread one."""
    savings.record(10_000, 0, "ripgrep", passthrough=False)
    for _ in range(5):
        savings.record(200, 100, "cargo", passthrough=False)
    led = _ledger()
    totals = led.totals()
    led.close()

    assert totals["largest_single_saving"] == 10_000
    assert totals["largest_share_pct"] == 95.2


def test_savings_survive_the_store_being_emptied(isolated_home):
    """The whole point: pruning the cache must not rewrite the history."""
    cfg = Config()
    store = Store()
    text = "\n".join(["npm warn deprecated pkg"] * 300)
    core.compress("npm install", text, cfg=cfg, store=store)
    before = _ledger()
    saved_before = before.totals()["tokens_saved"]
    before.close()

    store.conn.execute("DELETE FROM outputs")
    store.conn.commit()
    held = store.totals()["count"]
    store.close()

    after = _ledger()
    totals = after.totals()
    after.close()

    assert held == 0
    assert totals["tokens_saved"] == saved_before > 0


def test_filters_are_credited_by_what_they_saved(isolated_home):
    savings.record(1000, 100, "npm-install", passthrough=False)
    savings.record(500, 400, "cargo", passthrough=False)
    savings.record(300, 30, "npm-install", passthrough=False)
    led = _ledger()
    rows = led.by_filter()
    led.close()

    assert rows[0]["label"] == "npm-install"
    assert rows[0]["outputs"] == 2
    assert rows[0]["tokens_saved"] == 1170
    assert rows[1]["label"] == "cargo"


def test_the_counter_records_no_command_or_output(isolated_home):
    """Filter labels are Winnow's own vocabulary. Nothing of the user's is kept."""
    core.compress("git log --oneline /secret/path",
                  "\n".join(["deadbeef fix the thing"] * 200), store=None)
    led = _ledger()
    dump = json.dumps([
        list(r) for r in led.conn.execute("SELECT * FROM by_filter").fetchall()
    ])
    led.close()

    assert "secret" not in dump
    assert "deadbeef" not in dump


def test_a_broken_ledger_never_breaks_compression(isolated_home, monkeypatch):
    import sqlite3

    def explode(*a, **k):
        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(savings, "Savings", explode)
    result = core.compress("npm install", "\n".join(["npm warn x"] * 300), store=None)

    assert result.passthrough is False
    assert result.comp_tokens < result.raw_tokens


def test_reset_clears_both_tables(isolated_home):
    savings.record(1000, 100, "npm-install", passthrough=False)
    led = _ledger()
    led.reset()
    totals = led.totals()
    rows = led.by_filter()
    led.close()

    assert totals["outputs_seen"] == 0
    assert rows == []


def test_gain_reports_the_counter_and_the_store_separately(isolated_home, capsys):
    core.compress("npm install", "\n".join(["npm warn x"] * 300), store=Store())
    cli.cmd_gain(Namespace(history=False, limit=10, json=False))
    out = capsys.readouterr().out

    assert "lifetime counter" in out
    assert "of everything seen" in out
    assert "not a lifetime total" in out


def test_gain_json_carries_both_views(isolated_home, capsys):
    core.compress("npm install", "\n".join(["npm warn x"] * 300), store=Store())
    cli.cmd_gain(Namespace(history=False, limit=10, json=True))
    data = json.loads(capsys.readouterr().out)

    assert data["lifetime"]["outputs_compressed"] == 1
    assert data["store"]["outputs_held"] == 1
    assert data["by_filter"][0]["label"]
