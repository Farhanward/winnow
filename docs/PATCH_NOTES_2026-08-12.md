# Patch notes: 2026-08-12

Written while answering a narrower question: why does a Claude Code session burn
so many tokens when Winnow reports 84.8% savings on the same machine?

The short answer is that the savings were real but they belonged almost entirely
to Codex, and the Claude side was wrapping 8% of what it saw. Two defects caused
that, and a third gap meant the largest Claude-side consumers were never
reachable at all. All three are addressed here.

---

## The measurement

`wn gain` and `wn efficiency` at the start of the session:

```
winnow savings · 109 outputs compressed
  tokens in    :  103,954,432
  tokens out   :   15,781,148
  tokens saved :   88,173,284
  reduction    :        84.8%

runtime     seen/auto   runs  compressed   tokens in→out            saved
codex          2/2       939      47  108,650,822→21,351,930        80.3%
claude      2532/208     201      18       195,861→99,361           49.3%
gemini         0/0         0       0  no data
local        202/20       20       0        30,682→30,682            0.0%
```

`wn discover` named the two largest sinks ever recorded:

```
76,457,300 tok  rg -n -i --hidden --glob !node_modules/** --glob !target/**
25,723,627 tok  rg -n --hidden --glob !node_modules/** --glob !installer/win
```

Both are Codex shell calls, both already compressed. The headline reduction is
essentially those two lines.

The Claude row is the finding. Across its entire history, 195,861 tokens of
shell output passed through Winnow. A single session of subagent work in the
same period consumed roughly 1.5 million. Terminal output was not where the
tokens were going, and 2,532 commands seen against 208 wrapped said the hook was
watching almost everything and acting on almost nothing.

---

## Gap 1: the tools that spend the most were never matched

`Read`, `Grep` and `Glob` assemble their output inside the agent. There is no
command line to rewrite and no stream for a filter to read, so no hook can
compress what they return. That much was already documented and is still true.

What was missed is that a `PreToolUse` hook rewrites *inputs*, and capping an
input caps the output at the source. That does not require seeing the output at
all.

So the hook now matches `Read` and `Grep`:

- **Grep**: a `head_limit` is injected when the call carries none. 80 rows in
  `content` mode, 200 in `files_with_matches` and `count`. A content row is a
  full source line plus a `path:line:` prefix, and `-A`/`-B`/`-C` multiply it, so
  it costs several times what a path row costs. The tool's own default is 250 for
  every mode, which prices content and paths identically.
- **Read**: a 400-line `limit` is injected only when the file exceeds 128 KiB and
  the call carries no limit. The test is `os.path.getsize`, not a line count:
  this hook runs on every tool call, and opening a file to count its lines would
  put real I/O on the hot path to answer a question a size answers well enough.
  At roughly 60 bytes per line, 2,000 lines is about 120 KB, so below the
  threshold the tool's own 2,000-line cap is already doing the work and clamping
  would only truncate a cheap read. `offset` is never touched, since moving it
  returns different content rather than less of it.
- **An explicit value is always honoured**, including `head_limit: 0` meaning
  unlimited. A caller who wrote a number made a decision.
- A path that cannot be stat'd clamps nothing and raises nothing.

`Glob` is not matched and cannot be. Its schema carries `pattern` and `path` and
nothing else: no limit, no count, no cap of any kind. The only way to make it
return less is to rewrite the pattern, which changes what the caller asked for
rather than how much of it comes back. A comment in `hook.py` records this so the
next reader does not spend the time again.

Thresholds live in `~/.winnow/config.json` as `grep_head_limit_content`,
`grep_head_limit_paths`, `read_large_file_bytes` and `read_clamp_lines`. Zero
disables each.

New `inputs_seen` and `inputs_clamped` counters surface as a `clamped` column in
`wn efficiency`. They are deliberately outside every token and reduction figure.
Capping a request is not compressing an output, and folding the two together
would inflate a published number with work of a different kind.

---

## Gap 2: `2>&1` blocked wrapping on PowerShell

The PowerShell branch rejected any command matching `_UNSAFE_CHAIN`, which
included a bare `>`. Every `2>&1` contains one.

The Bash branch never had this problem because `_FILE_REDIRECT` uses `(?![&\d])`
to let stream merges through while still refusing redirection to a file, where
the bytes go to disk and there is nothing left to compress. The PowerShell branch
carried no such exclusion.

This also made the documentation wrong. The notes stated that `2>&1` is wrapped
normally, which was true for Bash and false for PowerShell, on a Windows
workstation where PowerShell is most of the output there is.

The fix points the PowerShell branch at the same `_FILE_REDIRECT` rule rather
than duplicating the logic. Doing so exposed a backtracking flaw in that shared
regex: `2>>&1` matched as `2>` followed by a file named `>&1`. The operator is
now matched as a whole run:

```
(?:^|\s)[\d*]?>{1,2}(?![>&])\s*(?![&\d])\S
```

which also covers PowerShell's `*>` forms.

---

## Gap 3: a leading `cd` defeated the eligibility check

`_wrapped` decided from `shlex.split(cmd)[0]`. For `cd C:\Projects\thing; cargo
test` that token is `cd`, which is not in the known-reader set, so the line was
skipped. On Windows this is the dominant shape, and combined with Gap 2 it
accounts for most of the 2,532-against-208 ratio.

`_tokens` now uses `shlex.shlex(posix=True, punctuation_chars=True)` with
`escape` and `commenters` disabled, so `;` and `&&` come back as their own tokens
while quoting is respected and Windows backslashes survive. `_eligibility` peels
one leading directory-change segment (`cd`, `chdir`, `sl`, `set-location`,
`pushd`, `push-location`) and decides from the first token of the next segment.

The `cd` is never stripped. The whole original line is still what gets wrapped
and executed, because the directory change is part of what the command means.

The guards were deliberately not narrowed to the inspected segment. `_MUTATING`
and `_FILE_REDIRECT` still read the entire line, so `cd X; rm -rf y` stays
unwrapped and fully visible. A line that cannot be tokenised confidently, such as
one with an unbalanced quote, is left alone rather than guessed at.

---

## Gap 4: `wn hook install` wrote only the first matcher

`cli._hook_install` appended `PreToolUse[0]` and stopped. The PowerShell matcher
never reached `settings.json` despite the README describing it, and the new
`Read` and `Grep` matchers would not have either. Anyone who installed through
the CLI rather than editing the file by hand got Bash coverage only.

Fixed in `winnow/cli.py`, with a test.

---

## Verification

Thirteen event shapes fed through `python -m winnow.hook` with `WINNOW_HOME`
isolated in a temp directory, so the published counters were not touched:

| Event | Before | After |
|---|---|---|
| PS `cargo test` | wrapped | wrapped |
| PS `cd X; cargo test` | not wrapped | wrapped |
| PS `cargo test 2>&1 \| Select-String` | not wrapped | wrapped |
| PS `cd X; cargo test 2>&1 \| Select-String` | not wrapped | wrapped |
| PS `cargo test 2>>&1` | not wrapped | wrapped |
| PS `cargo test > out.txt` | not wrapped | not wrapped |
| PS `cd X; Remove-Item -Recurse -Force y` | not wrapped | not wrapped |
| PS `cd X; frobnicate --all` | not wrapped | not wrapped |
| PS unbalanced quote | not wrapped | not wrapped |
| Bash `cd X && cargo test` | not wrapped | wrapped |
| Bash `cd X && rm -rf y` | not wrapped | not wrapped |
| Bash `cargo test \| head -20` | wrapped | wrapped |
| Bash `cargo test > out.txt` | not wrapped | not wrapped |

Input clamping, same method:

| Event | Result |
|---|---|
| `Grep` content mode, no head_limit, Windows path | `head_limit: 80`, path preserved |
| `Grep` files_with_matches, no head_limit | `head_limit: 200` |
| `Grep` with explicit `head_limit: 0` | untouched |
| `Read` of a 200 KB file, no limit | `limit: 400` |
| `Read` of a small file | untouched |
| `Read` with an explicit limit | untouched |
| `Read` of a missing path | untouched, no exception |
| `Glob` | untouched |
| `Bash` `cargo test --workspace` | wrapped, no regression |

Suite: 67 passed.

---

## Still out of reach

- **`Glob`.** No limit parameter exists to clamp. Not a matter of effort.
- **Output of any in-agent tool.** `PreToolUse` rewrites inputs. Nothing in the
  hook contract exposes a tool's output for filtering before the agent reads it.
- **Gemini inside Antigravity.** The editor supports no hooks at all, so nothing
  can rewrite its commands. The only routes are the explicit
  `wn run --client gemini -- <cmd>` form or a `WINNOW_CLIENT=gemini` variable
  scoped to that terminal. Setting it machine-wide would mislabel every Claude
  and Codex run, so it stays a per-terminal choice. The counters still read
  `0/0`.
- **Hook changes need a fresh session.** Hooks are read at session start, so the
  `Read` and `Grep` matchers do not affect a session already running. The
  `clamped` column reading `0/0` immediately after installation is expected.

---

## Files changed

`winnow/hook.py`, `winnow/efficiency.py`, `winnow/config.py`, `winnow/cli.py`,
`tests/test_winnow.py`, `tests/test_efficiency.py`, `README.md`, `CHANGELOG.md`.

Every test that exercises the hook isolates `WINNOW_HOME` into a temp directory.
No existing test was writing to the real home. The stats file gains its new
columns through an in-place migration on open, and `snapshot()` filters unknown
columns, so a file written by either an older or a newer build still loads.
