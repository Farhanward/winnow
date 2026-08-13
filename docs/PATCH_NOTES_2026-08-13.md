# Patch notes: 2026-08-13

A review of the previous day's work against the code rather than against its
own report. Two defects came out of it, both narrow, both in paths the earlier
patch had touched. Neither breaks a command, and that is why neither showed up
in the test suite: they cost tokens and reliability rather than correctness.

Also here: the published counters, refreshed, and a measurement of what the
whole arrangement actually saves.

---

## Defect 1: a redirect target beginning with a digit

`_FILE_REDIRECT` decides whether a line sends its bytes to disk. It excluded a
digit after the operator:

```python
r"(?:^|\s)[\d*]?>{1,2}(?![>&])\s*(?![&\d])\S"
```

The intent was to leave stream merges alone, and that is right, but the digit
that marks a stream sits *before* the operator (`1>&2`), where `[\d*]?` and the
`(?![>&])` guard already handle it. A digit *after* the operator is the first
character of a filename. So `cargo tree > 1.log` and `cargo tree >2026.txt` read
as merges, and their commands went through the compressor with every byte
already headed for disk.

Nothing broke: the redirect still happened inside the shell. The cost was a
process spawn and a run recorded against a command with an empty stdout, which
inflates the `runs` column on the table this project publishes.

The fix is one character. `\d` comes out of the trailing guard:

```python
r"(?:^|\s)[\d*]?>{1,2}(?![>&])\s*(?!&)\S"
```

Checked against eleven shapes. `> out.txt`, `> 1.log`, `>2026.txt`, `>> log.txt`
and `2> err.txt` are redirects. `2>&1`, `2>>&1`, `*>&1`, `> &1` and `1>&2` are
not. A test covers both halves so the guard cannot quietly widen again.

## Defect 2: a hand-edited config value took the hook down

`Config.load` walks the JSON and assigns whatever it finds through `setattr`,
with no type check. `_clamp_input` caught `OSError` around the load and nothing
around the use, so a value that is not a number reached `int()` and raised.

```
TypeError: int() argument must be ... not 'NoneType'
```

On every `Read` and every `Grep`, for as long as the file stayed that way. The
route in is not exotic: the README says setting a threshold to `0` turns that
clamp off, and `null` is what a reader reaches for next.

The clamping call is now wrapped, and a value that cannot be read turns off its
own clamp while the others keep working. This follows what the module already
does for its counters: measurement and tuning must never fail the tool call they
sit in front of.

---

## What it saves

Three separate questions, measured separately. Every run below used a
`WINNOW_HOME` in a temp directory, so the published counters were not touched.

**Compression, on output with something to compress.** `benchmarks/benchmark.py`
against the real pipeline:

| Case | Before | After | Saved |
|---|---:|---:|---:|
| npm warning wall | 3,060 | 37 | 98.8% |
| pip satisfied chatter | 1,963 | 19 | 99.0% |
| JSON API response | 42,079 | 347 | 99.2% |
| repetitive server log | 4,445 | 40 | 99.1% |

**Overhead, on output with nothing to compress.** Each command was run twice
with the same argv, once bare and once behind `wn run`, with stdout and stderr
captured on both sides:

| Case | Bare | Winnow | Delta |
|---|---:|---:|---:|
| `cd X; git log --oneline -60` | 300 | 301 | -0.3% |
| `cd X; python -m pytest -q 2>&1` | 122 | 123 | -0.8% |
| `pip list 2>&1` | 1,150 | 1,150 | 0.0% |
| `cd X; pip install --dry-run` | 317 | 273 | +13.9% |
| `git -C X log --oneline -60` (control) | 202 | 203 | -0.5% |

A passthrough costs about one token. That is the number that matters for the
wrapping fixes, because widening coverage is only worth doing if the commands it
brings in are close to free when there is nothing to strip.

An earlier version of this measurement reported a 98-token penalty per command.
That was the harness, not Winnow: it compared the bare command's stdout against
the wrapped command's stdout *and* stderr, and the nested PowerShell it used to
run one side wrote a CLIXML progress block to stderr worth about 98 tokens. Same
argv on both sides, both streams on both sides, and the penalty disappears.

**Input clamping, which is where the volume is.** Nothing is compressed here, so
these are counted apart from every figure above:

| Clamp | Uncapped | Capped | Saved |
|---|---:|---:|---:|
| `Read`, one real file past the threshold | 22,282 | 4,358 | 80.4% |
| `Grep`, content mode, real search | 4,039 | 1,628 | 59.7% |

The `Read` row compares 400 lines against the 2,000 the tool would have returned
on its own, not against the whole file, which is the honest baseline: the tool
was never going to return more than 2,000.

---

## Coverage, and the number worth publishing

`seen/auto` for Claude Code covers two eras and should be read that way:

| Period | Inspected | Wrapped | Rate |
|---|---:|---:|---:|
| Before the wrapping fixes | 2,550 | 213 | 8.4% |
| Since | 444 | 191 | 43% |
| Cumulative, as the table prints it | 2,994 | 404 | 13.5% |

The cumulative figure averages a broken period with a working one and understates
the fix threefold. The README now carries the split.

The same widening explains the drop in Claude's `saved`, from 61.6% to 41.8%.
The commands the fix brought in are mostly short ones that print a few lines, so
they pull the mean down while adding almost nothing to the total. The absolute
saving has not moved backwards at any point.

---

## Verified wiring

`wn hook install` writes all four matchers as of the previous patch, and the
installed `settings.json` on the development machine carries `Bash`,
`PowerShell`, `Read` and `Grep`, each pointing at `python -m winnow.hook`. Every
command in the overhead table above was routed by running that hook as a
subprocess against a real PreToolUse event, so what the table measures is the
automatic path, not a direct call into the library.

---

## Files changed

`winnow/hook.py`, `tests/test_winnow.py`, `README.md`, `CHANGELOG.md`,
`docs/PATCH_NOTES_2026-08-12.md`.

69 tests pass.
