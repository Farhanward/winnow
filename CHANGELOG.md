# Changelog

All notable changes to Winnow are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
  present.
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
