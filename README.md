# 🌾 Winnow

**Winnow the noise out of your CLI output — keep the grain, blow away the chaff, and stash the chaff so you can recall it anytime.**

Winnow is a token-optimizing proxy for command-line output. It sits between a command and whatever reads its output — an AI coding agent, a log pipeline, or you — and shrinks noisy output (install logs, JSON dumps, test runs, server logs) by **60–99%**, while keeping a full, searchable copy on disk so nothing is ever truly lost.

It's built for the age of LLM coding agents, where every line a tool prints costs tokens, money, and context-window space. But it's just as useful for anyone who's tired of scrolling past 500 lines of `npm warn`.

```
$ wn run -- npm install
added 512 packages, and audited 513 packages in 8s
72 packages are looking for funding
found 0 vulnerabilities
… ⟨45 npm warn/notice lines hidden⟩
⟨winnow npm-install: 561→36 tok, saved 94% · full: wn recall a1b2c3⟩
```

---

## Why

Command output is mostly noise. A package install prints a wall of deprecation warnings around one line that matters. An API response is 5,000 array elements when you needed the shape. A log tail repeats the same message 200 times. Feeding all of that to an LLM (or your eyes) is pure waste.

Winnow removes the waste **safely**: it never throws anything away. Every full output is teed to a local store first, addressable by a short handle (`a1b2c3`) and full-text searchable. Compression is a *view*; the original is one command away (`wn recall a1b2c3`).

---

## Install

```bash
pip install winnow-cli
```

Optional, for exact token counts instead of the built-in heuristic:

```bash
pip install "winnow-cli[tokens]"   # adds tiktoken
```

Requires Python 3.9+. No other runtime dependencies beyond PyYAML.

---

## Quick start

**Wrap a command** — Winnow runs it, compresses the output, and prints a footer telling you how much it saved and how to get the full text back:

```bash
wn run -- pip install -r requirements.txt
wn run -- docker logs api
wn run -- curl -s https://api.example.com/users
```

**Filter piped text** — compress something already produced:

```bash
kubectl logs pod-xyz | wn filter --cmd "kubectl logs"
cat huge-response.json | wn filter
```

**Recall the full output** — by handle, or by searching everything Winnow has ever seen:

```bash
wn recall a1b2c3               # print the full original
wn recall a1b2c3 --lines 40-80 # just those lines
wn recall "connection refused" # full-text search across history
```

**See your savings:**

```bash
wn gain            # totals
wn gain --history  # per-output breakdown
```

**Skim a source file** down to its structure:

```bash
wn skim app/models.py   # imports + signatures + docstrings, bodies elided
wn skim response.json   # schema + samples instead of the whole payload
```

---

## How it works

Winnow runs output through layered passes and keeps the result only if it's a clear win. For any command:

1. **Tee** — the full raw output is written to the recall store *before* anything else. This is the safety net that makes aggressive compression risk-free.
2. **Structure-aware** — if the output is **JSON**, arrays are truncated to a representative sample, long strings are clipped, and deep nesting is elided, preserving the shape (keys and types). Line-based passes are skipped so the structure isn't corrupted.
3. **Built-in filters** — fast, hand-written transforms for a handful of extremely common, extremely noisy commands (`npm/pip/yarn install`, `git status`, `pytest`, directory listings).
4. **Declarative rules** — YAML rule packs (built-in + your own) that match a command with a regex and run ordered actions: drop lines, fold repeats, guard against error cascades, clip long lines, keep head/tail, and more.
5. **Safety valve** — if the result doesn't save at least a configurable fraction of tokens (default 15%), or the output was tiny to begin with, Winnow passes the **original through untouched**. It compresses when it helps and gets out of the way when it doesn't.

The model (or you) sees the compressed body plus a one-line footer:

```
⟨winnow <what-acted>: <before>→<after> tok, saved <pct>% · full: wn recall <handle>⟩
```

### Fold and fingerprint

Repetitive output is normalized into *fingerprints* — numbers, hashes, UUIDs, timestamps and addresses are masked so near-identical lines collapse together:

```
2026-07-22 10:00:01 INFO worker heartbeat ok id=1000
    … ⟨×200 similar lines⟩
2026-07-22 10:30:00 ERROR db timeout after 30000ms retry=0
    … ⟨×60 similar lines⟩
```

---

## Use it with an AI coding agent

Winnow shrinks the tool output your agent has to read, so more of the context window goes to actual work.

**Simplest:** tell the agent to prefix heavy commands with `wn run --`.

**Automatic (Claude Code):** wire up the bundled PreToolUse hook, which rewrites eligible `Bash` commands to the wrapped form for you. It only touches simple, read-heavy commands and never rewrites anything with shell metacharacters, so it can't change the meaning of a pipeline.

```bash
wn hook show                # print the settings.json snippet
wn hook install             # merge it into ~/.claude/settings.json
```

**Other agents / editors:** any tool that lets you wrap or alias shell commands can call `wn run -- <command>`. The compressed output — footer included — goes to stdout unchanged, and the child's exit code is preserved, so it's a transparent drop-in.

---

## Customize with rules

Rules are plain YAML, so you can tune Winnow to your own tools without writing code. Drop a `*.yaml` file in your rules directory:

```bash
wn rules path     # prints the directory ($WINNOW_HOME/rules)
wn rules list     # show all loaded rules and built-in filters
wn rules test --cmd "terraform apply"   # see what would match
```

Example — quiet down a chatty deploy tool:

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

Supported actions: `drop_lines`, `keep_lines`, `replace`, `collapse_repeats`, `cascade_guard`, `keep_head_tail`, `max_line_len`, `strip_ansi`, `summary`. User rules load on top of the built-in packs.

---

## Configuration

Winnow keeps everything under `$WINNOW_HOME` (default `~/.winnow`): the recall store and your rule packs. Tunables live in `$WINNOW_HOME/config.json`:

| Key | Default | Meaning |
|-----|---------|---------|
| `min_saving` | `0.15` | Minimum fraction saved to keep the compressed version |
| `min_tokens` | `40` | Outputs smaller than this are never compressed |
| `collapse_threshold` | `3` | Fold runs of at least this many similar lines |
| `keep_head` / `keep_tail` | `40` / `20` | Default head/tail sizes for trims |
| `max_store_rows` | `5000` | Recall store cap (oldest pruned first) |

---

## Measured results

On purpose-built noisy outputs (the cases Winnow targets):

| Command | Before → After | Saved |
|---------|----------------|-------|
| `npm install` (warning wall) | 561 → 36 tok | **94%** |
| `pip install` (download chatter) | 247 → 31 tok | **87%** |
| JSON API response (5k-item array) | 25,359 → 637 tok | **97%** |
| Server log (repetitive + cascade) | 3,597 → 41 tok | **99%** |

On a corpus of *real, mixed* command output (hundreds of everyday commands), the aggregate is more modest — because Winnow safely passes small and already-terse outputs straight through — while still averaging **~47% on the outputs it does compress**. It saves big where there's noise, and does no harm where there isn't.

*(Token figures use the built-in heuristic; install the `tokens` extra for exact tiktoken counts.)*

---

## Commands

| Command | Does |
|---------|------|
| `wn run -- <cmd>` | Run a command and compress its output |
| `wn filter [--cmd L]` | Compress text piped on stdin |
| `wn recall <handle\|query>` | Fetch a stored output, or search history |
| `wn gain [--history]` | Token-savings analytics |
| `wn skim <file>` | Structural skeleton of a `.py`/`.json` file |
| `wn discover` | Show the biggest token sinks seen |
| `wn rules [list\|path\|test]` | Inspect the rule engine |
| `wn hook [show\|install\|run]` | Agent/editor integration |

---

## Contributing

Contributions are welcome from everyone. New built-in filters, rule packs for tools you use, better structural compressors, a native port — all fair game. Open an issue or a PR.

```bash
git clone https://github.com/Farhanward/winnow
cd winnow
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE) — free to use, modify, extend, and redistribute, for everyone.
