<p align="center">
  <img src="assets/winnow-social-preview.png" alt="Winnow: compress noisy CLI output and recall every original" width="100%">
</p>

# Winnow

[![CI](https://github.com/Farhanward/winnow/actions/workflows/ci.yml/badge.svg)](https://github.com/Farhanward/winnow/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/v/release/Farhanward/winnow)](https://github.com/Farhanward/winnow/releases)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-61d685)](LICENSE)

**Local-first CLI output compression for Codex, Claude Code, and your shell.**
Winnow removes repetitive noise before an agent reads it, makes zero LLM calls,
and keeps the complete original output in a searchable local store.

```text
$ wn run -- npm install
added 512 packages, and audited 513 packages in 8s
found 0 vulnerabilities
... <180 npm warn/notice lines hidden>
<winnow npm-install: 3,060->37 tok, saved 99% - full: wn recall a1b2c3>
```

## Install

Install the latest release directly from GitHub:

```bash
pipx install "git+https://github.com/Farhanward/winnow.git@v0.1.1"
```

Or with `uv`:

```bash
uv tool install "git+https://github.com/Farhanward/winnow.git@v0.1.1"
```

For an editable development install:

```bash
git clone https://github.com/Farhanward/winnow.git
cd winnow
python -m pip install -e ".[dev]"
```

Requires Python 3.9+. The only required runtime dependency is PyYAML. Install
the `tokens` extra for exact `tiktoken` counts:

```bash
python -m pip install "winnow-cli[tokens] @ git+https://github.com/Farhanward/winnow.git@v0.1.1"
```

## See the difference

<p align="center">
  <img src="assets/winnow-demo.png" alt="Raw npm output compared with Winnow's compact, recallable view" width="100%">
</p>

Run any noisy command through Winnow:

```bash
wn run -- pip install -r requirements.txt
wn run -- docker logs api
wn run -- curl -s https://api.example.com/users
```

Filter output that already exists:

```bash
kubectl logs pod-xyz | wn filter --cmd "kubectl logs"
cat huge-response.json | wn filter
```

Recall or search the full original:

```bash
wn recall a1b2c3
wn recall a1b2c3 --lines 40-80
wn recall "connection refused"
```

## Reproducible benchmark

Run `python benchmarks/benchmark.py` to execute Winnow's real pipeline against
deterministic synthetic fixtures:

| Synthetic case | Before | After | Output tokens saved |
|---|---:|---:|---:|
| npm warning wall | 3,060 | 37 | **98.8%** |
| pip satisfied chatter | 1,963 | 19 | **99.0%** |
| JSON API response | 42,079 | 347 | **99.2%** |
| repetitive server log | 4,445 | 40 | **99.1%** |

These are deliberately noisy target cases, measured with the built-in
character heuristic. They measure compression of individual command output,
not an equal reduction in an agent's total context use, API bill, or task cost.
See [benchmarks/README.md](benchmarks/README.md) for the method.

## Why Winnow

| Capability | Winnow | Basic truncation |
|---|:---:|:---:|
| Full original remains locally recallable | Yes | No |
| Search across captured output | Yes | No |
| Command-aware filters | Yes | No |
| Structure-aware JSON compression | Yes | No |
| User-defined YAML rules | Yes | No |
| Requires an LLM or network request | No | No |
| Automatic Claude Code and Codex hook | Yes | No |

Winnow is a reversible view over command output. It tees the full result to a
local SQLite store first, applies structural and declarative filters, and uses a
safety valve: small outputs or weak reductions pass through unchanged.

## Automatic agent integration

Winnow includes a conservative PreToolUse hook. It wraps eligible read-heavy
commands and leaves unknown or mutating commands alone.

```bash
wn hook show
wn hook install
```

`wn hook install` merges the hook into Claude Code's user settings. For Codex,
merge the output of `wn hook show` into `~/.codex/hooks.json`, enable hooks, and
review the hook in `/hooks`. On Windows, eligible PowerShell pipelines are
encoded before wrapping so the original script remains intact.

## How the pipeline works

1. **Store first:** write the complete raw output to the local recall store.
2. **Preserve structure:** compress JSON by shape instead of line slicing.
3. **Use command context:** apply built-in filters for npm, pip, pytest, git,
   and directory listings.
4. **Apply rules:** run bundled and user-authored YAML passes for repeated
   lines, error cascades, ANSI noise, and long output.
5. **Check the win:** return the original when compression saves less than the
   configured threshold.

Everything lives under `$WINNOW_HOME` (default `~/.winnow`). The store is local
and may contain sensitive command output, so protect it like shell history.

## Commands

| Command | Purpose |
|---|---|
| `wn run -- <cmd>` | Run a command and compress its output |
| `wn filter [--cmd LABEL]` | Compress stdin |
| `wn recall <handle or query>` | Fetch or search full originals |
| `wn gain [--history]` | Show token-reduction analytics |
| `wn skim <file>` | Reduce Python or JSON to its structure |
| `wn discover` | Find the largest stored token sources |
| `wn rules [list, path, test]` | Inspect rule packs |
| `wn hook [show, install, run]` | Configure agent integration |

## Custom rules

Add `*.yaml` files to the directory shown by `wn rules path`:

```yaml
- name: my-deploy
  match: 'deploy\.sh'
  actions:
    - strip_ansi: true
    - drop_lines: '^(DEBUG|TRACE) '
    - collapse_repeats: 3
    - keep_head_tail: [30, 40]
    - summary: 'hid {dropped} deploy log lines'
```

Supported actions include `drop_lines`, `keep_lines`, `replace`,
`collapse_repeats`, `cascade_guard`, `keep_head_tail`, `max_line_len`,
`strip_ansi`, and `summary`.

## Contributing

Issues and focused pull requests are welcome, especially sanitized examples of
noisy commands that deserve a safe filter. Read [CONTRIBUTING.md](CONTRIBUTING.md)
and report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
