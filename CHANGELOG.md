# Changelog

All notable changes to Winnow are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
