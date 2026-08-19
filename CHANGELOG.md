# Changelog

All notable changes to Winnow are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `wn agent audit` and `wn agent tools`, which measure the agent's own token
  budget rather than its terminal output. They read the JSONL transcripts the
  agents already write, send nothing anywhere, and report the counts the
  provider returned. Three runtimes, three readers: Claude Code under
  `~/.claude/projects` with usage on every assistant message and a sidechain
  flag for subagents; Codex under `~/.codex/sessions`, whose `token_count`
  events name things differently and fold the cached prefix inside
  `input_tokens`, so the prompt total comes from that column alone rather than
  from a sum that would count the prefix twice; and Gemini in Antigravity
  under `~/.gemini/antigravity/brain`, which records no usage counters at all
  and is therefore reported by activity with its token columns reading `no
  counters` rather than `0`, since a zero would say the runtime was free.
  Antigravity writes each session twice and the duplicate is skipped. Format
  detection is on line shape rather than on path, and a file matching none of
  the three is counted as unrecognised rather than parsed anyway. `audit` separates
  the session floor, the smallest whole prompt any request in a session paid,
  from the work done on top of it, and multiplies that floor by the request
  count to show the fixed cost of the prompt prefix, summed per runtime
  because Claude and Codex sit at different floors and one blended median
  times the combined request count would report a cost neither of them paid. It reports subagent
  requests as their own line, and lists the tools that returned the most bytes
  into context. `tools` names the MCP servers and plugins that sit in the floor
  of every request and were never called. Both take `--days N` and `--json`.
  Neither edits the agent's configuration: the report ends at the names, and
  the edit stays with the reader.
- `max_store_bytes` (256 MiB) and `max_row_bytes` (4 MiB) for the recall store.
  The existing `max_store_rows` cap counts rows, and the outputs that fill a
  disk are not numerous but large: one `rg` sweep is a single row holding
  hundreds of megabytes, which is how a store reached 1.04 GB at 127 rows, 2.5%
  of its row cap. The byte cap drops the oldest rows one at a time until the
  payload fits; the row cap keeps the head of one output and records in the
  stored text how much was dropped, cutting on a character boundary. Token
  counts are never rewritten by a cap, so `wn gain` reports the same figures
  either way. 0 turns a cap off, and a value that cannot be read turns off its
  own cap and leaves the others working.
- Input clamping for Claude Code's `Read` and `Grep`. Their output is assembled
  inside the agent, where no hook can compress it, so the PreToolUse hook caps
  the request instead: a `head_limit` on a `Grep` that carries none (80 rows in
  `content` mode, 200 for the path-listing modes), and a 400-line `limit` on a
  `Read` of a file over 128 KiB that carries none. A limit the caller wrote is
  always passed through, including `head_limit: 0` for unlimited. The size test
  is a `stat` rather than a line count, since the hook runs on every tool call,
  and a path that cannot be stat'd is left alone. `Glob` takes neither a limit
  nor a count, so nothing there can be capped without changing the pattern, and
  it is not matched. Thresholds live in `~/.winnow/config.json`.
- `inputs_seen` and `inputs_clamped` counters, shown as a `clamped` column in
  `wn efficiency` and as keys in `wn efficiency --json`. They are kept out of
  the token and reduction figures: capping a request is not compressing an
  output. A stats file written before these columns existed is migrated in place
  and keeps its numbers.
- Aggregate-only efficiency collection for Codex, Claude Code, and local
  agents, with text and JSON reports through `wn efficiency`.
- Automatic runtime tags in agent hook rewrites.
- `cargo-json` rule for `--message-format=json` builds. It drops
  `compiler-artifact` and `build-script-executed` bookkeeping and keeps
  diagnostics, which removed 99.8% of a measured 131 KB `cargo check` run.
- `rustc-diagnostics` rule folding the blank gutter lines rustc prints between
  spans.

### Fixed

- A clamped `Read` or `Grep` told the model nothing. The hook rewrote the tool
  input and the model received 400 lines of a file it had asked for in full,
  with nothing saying so. The failure mode is a wrong conclusion rather than a
  missing line: a 12,000-line file read to line 400 looks like a file that ends
  at line 400. The hook now returns `additionalContext` beside `updatedInput`,
  naming what was capped and how to get the rest. An unclamped call sends no
  note, and a limit the caller wrote is still never second-guessed.
- `compress_json` replaced anything past the depth limit with a bare `"…"`,
  which hid how much was dropped, what shape was dropped, and that anything was
  dropped at all, since a plain string is indistinguishable from a value that
  genuinely is an ellipsis. It now names the elision, as `⟨winnow: object with
  12 keys elided at depth limit⟩`. A scalar at the depth limit is kept instead
  of elided: replacing the number 42 with an ellipsis costs a reader
  information and saves nobody anything.
- The recall store grew without bound on disk. `max_store_rows` was the only
  cap and it counts rows, so a store holding one `rg` sweep per row passed a
  5000-row check at 127 rows and 1.04 GB. It is now capped by size as well, and
  a prune vacuums the file so the freed pages go back to the filesystem instead
  of sitting in SQLite's free list. The vacuum runs only when the free space is
  worth the rewrite, since VACUUM rewrites the whole database and would
  otherwise be felt on the command being wrapped.
- `2>&1` blocked wrapping on the PowerShell path. The chain guard rejected any
  `>`, and a stream merge contains one, so the most common way an agent asks
  for stderr was never compressed. Both shells now use the same file-redirect
  rule, which also stopped reading `2>>&1` as a redirect to a file named `>&1`.
  Redirection that lands in a file still bails.
- A leading `cd` hid the real command from the eligibility check. The first
  token of `cd C:\X; cargo test` is `cd`, which is in no wrap set, so the
  dominant command shape on Windows was skipped on both shells. Eligibility is
  now read from the segment after a leading directory change, joined by `;` or
  `&&`. The `cd` is not stripped and the whole line still runs as one unit. The
  mutating guard and the file-redirect guard still read the whole line, so
  `cd X; rm -rf y` stays visible, and a line that cannot be split confidently is
  left alone.
- `wn hook install` wrote only the first matcher into settings.json, so the
  PowerShell matcher never arrived through it even though the README said it
  did. It now merges every matcher the snippet carries, skipping ones already
  present. Anyone who installed through the CLI before this has Bash coverage
  only and should run `wn hook install` again.
- A redirect target beginning with a digit was read as a stream merge. The
  file-redirect pattern excluded a digit after the operator, so `> 1.log` and
  `>2026.txt` looked like `>&1` and their commands were wrapped with every byte
  already on its way to disk. The digit that marks a stream sits before the
  operator, as in `1>&2`, and that case was already covered.
- A non-numeric value in `config.json` raised on every `Read` and `Grep`.
  `Config.load` assigns what it finds without checking the type, so a `null`
  written by someone following the README's note that `0` disables a clamp
  reached `int()` and failed the tool call it was meant to shrink. A value that
  cannot be read now turns off its own clamp and leaves the others working.
- The PreToolUse hook skipped every command containing a shell metacharacter on
  the non-PowerShell path, so agents that pipe by habit (`… | tail -25`) were
  never wrapped. On one measured Claude Code install this left 775 of 778
  observed commands uncompressed. Pipelines and chains now go through `sh -c`.
  Redirection to a file stays unwrapped, and the PowerShell path used by Codex
  is unchanged.

## [0.1.1] - 2026-07-24

### Added

- Automatic PreToolUse integration for Claude Code and Codex.
- PowerShell-safe wrapping for eligible read-heavy Codex commands.
- Per-output history in `wn gain --history`.
- Reproducible synthetic benchmark suite.
- Cross-platform CI, community templates, and trusted PyPI publishing workflow.

### Fixed

- `wn gain --history` now uses the stored filter label correctly.
- Hook wrapping preserves PowerShell pipelines without touching mutating commands.
- Piped UTF-8 output from PowerShell no longer leaves its first BOM-prefixed
  noise line unfiltered.

## 0.1.0 - 2026-07-22

### Added

- Initial local-first CLI output compression pipeline.
- Built-in filters, YAML rules, searchable recall store, and token analytics.

[Unreleased]: https://github.com/Farhanward/winnow/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Farhanward/winnow/releases/tag/v0.1.1
