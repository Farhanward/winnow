# Patch notes — 2026-08-03

Branch: `fix/claude-pipeline-wrapping`

Written while diagnosing why the Claude Code runtime reported 0.0% savings in
`wn efficiency` while Codex reported 80.4% on the same machine.

---

## What was wrong

`wn efficiency` on 2026-08-03:

```
runtime    seen/auto   runs  compressed   tokens in→out     saved
codex         1/1       925      47  108,632,511→21,333,619   80.4%
claude      778/3         3       0             699→699        0.0%
```

The hook was firing on every Claude Code Bash call, but wrapping almost nothing.

### Cause 1: the metacharacter guard excluded an entire usage style

`_wrapped()` rejected any command containing a character in
`_META = set("|&;<>` + "`" + `$(){}")` on the non-PowerShell path. Agents that
pipe by habit never got wrapped. Every `cargo check 2>&1 | tail -25` and
`git log | head` was skipped.

Codex escaped this because `powershell=os.name == "nt" and is_codex` routes it
to the PowerShell branch, which base64-encodes the whole script and permits
pipes. The asymmetry lived in the code, not in user configuration.

### Cause 2: no rule matched dense JSON build output

Measured on a small Rust workspace:

| Command | Raw | Through Winnow (before) |
|---|---|---|
| `cargo tree` | 18,455 B | 18,844 B |
| `cargo check --workspace --message-format=json` | 131,103 B | 131,288 B |

Both grew. The `cargo` rule only drops `Compiling|Downloaded|Downloading|Updating`
lines and collapses repeats, and JSON output contains neither.

---

## Changes

### `winnow/hook.py`

Added `_FILE_REDIRECT` and a `shell_wrap` path. Commands with metacharacters are
now wrapped as `wn run --client <c> -- sh -c '<original line>'` using
`shlex.quote`, instead of being dropped.

Still unwrapped, deliberately:

- Redirection to a file (`> out`, `>> log`, `1> out`). The bytes go to disk.
  `2>&1` is explicitly excluded from this check so stream merges keep working.
- Commands whose first token is outside `_WRAP`.
- Commands matching `_MUTATING`.

The PowerShell branch is untouched. A regression test asserts that Codex output
still contains `-EncodedCommand` and never contains `sh -c`.

### `winnow/rules_data/20-build.yaml`

Two rules added:

- `cargo-json` — matches `cargo .*--message-format[ =]\S*json`, drops
  `compiler-artifact` and `build-script-executed` records, keeps
  `compiler-message`.
- `rustc-diagnostics` — drops rustc's blank gutter lines (`^\s*\|\s*$`).

### `tests/test_winnow.py`

`test_hook_skips_pipelines` asserted the old behavior and was replaced by:

- `test_hook_wraps_pipelines_through_sh`
- `test_hook_skips_redirection_to_file`
- `test_hook_still_skips_unwrappable_first_tokens_in_pipelines`
- `test_powershell_path_is_unaffected_by_sh_wrapping`

---

## Measured after the change

Same workspace, same command:

```
RAW    : 131,103 bytes
WINNOW :     284 bytes
SAVED  : 130,819 bytes (99.8%)
```

Compiler diagnostics survive. Verified by introducing a deliberate `E0308` and
confirming the rendered error text appears in the compressed output.

Full suite: 38 passed.

---

## Open items for upstream cleanup

1. The `sh -c` path assumes a POSIX shell on `PATH`. True for Claude Code (Git
   Bash on Windows) but worth a config toggle before release.
2. `cargo tree` is outside the `cargo` rule's `match`
   (`build|run|test|check|clippy`). Either widen the pattern or accept that
   dependency trees are not compressible.
3. Wrapping adds a small constant footer. On tiny outputs the wrapped form is a
   few hundred bytes larger than raw. A minimum-size threshold before wrapping
   would avoid the counters recording negative savings.
4. The hook only sees shell tools. File reads and grep-style tool calls made
   through an agent's native tools bypass Winnow entirely, which caps the
   achievable savings for agents that prefer native tools over shell commands.

---

## Note on benchmark data

All measurements in this document were taken with `HOME` pointed at a scratch
directory so the published efficiency counters were not polluted. Three earlier
exploratory runs (before that isolation was in place) did land in the real
counters under the `claude` runtime, each recording 0% savings. Reset the
`claude` counters before publishing benchmark figures if those three runs matter.
