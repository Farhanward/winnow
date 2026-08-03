# Changelog

All notable changes to Winnow are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Aggregate-only efficiency collection for Codex, Claude Code, and local
  agents, with text and JSON reports through `wn efficiency`.
- Automatic runtime tags in agent hook rewrites.
- `cargo-json` rule for `--message-format=json` builds. It drops
  `compiler-artifact` and `build-script-executed` bookkeeping and keeps
  diagnostics, which removed 99.8% of a measured 131 KB `cargo check` run.
- `rustc-diagnostics` rule folding the blank gutter lines rustc prints between
  spans.

### Fixed

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
