# Patch notes: 2026-08-19

Winnow compresses terminal output. This patch adds the measurement that says
how much that is worth, and the answer is uncomfortable enough to be the point
of the release.

Every output Winnow has compressed for Claude Code on the development machine
comes to 236,177 tokens read. The same machine's transcripts record 4.59
billion tokens of context read back across 14,955 billed requests. The channel
Winnow was built to guard carries about one twenty-thousandth of the bill.

So this release adds a second half: `wn agent`, which audits the agent's own
budget, and the recall store gets the size caps it should have had from the
start.

---

## Where the budget actually goes

Claude Code writes one JSONL file per session under `~/.claude/projects`, and
every assistant message in it carries the usage counters the API returned:
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, and
`cache_read_input_tokens`. Codex keeps its own sessions under
`~/.codex/sessions`. Nothing has to be instrumented, and nothing has to be
sent anywhere. The accounting is already on disk.

Reading it turns up two costs, neither of which is an output:

**The floor.** Every request re-reads the whole prompt prefix: system prompt,
memory files, skill descriptions, and the tool schema of every connected MCP
server. `wn agent audit` takes the smallest whole prompt any request in a
session paid and calls that the session's floor. The median floor here is
53,582 tokens. It is paid on the first request of a session and on the four
hundredth, so the real number is the floor times the request count: 801
million tokens on this machine, against 236,177 for everything Winnow ever
compressed on the same runtime.

**Subagents.** Each one starts cold and bills its own requests against the same
budget. Here they are 2,760 of 14,955 requests and 8.7% of the context read.
That is smaller than the floor and worth seeing separately, which is why the
audit reports it as its own line rather than folding it into a total.

### Three runtimes, three readers

The first cut of this read Claude Code only, and counted the other transcripts
as files while extracting nothing from them, which is worse than not reading
them: the file count implied a coverage that was not there.

Claude Code puts usage on every assistant message and flags subagent work with
`isSidechain`. Codex emits `token_count` events of its own and names the
columns differently, and its `input_tokens` already contains the cached prefix
that `cached_input_tokens` reports, so the prompt total is that one column
rather than a sum. Getting this wrong would have inflated every Codex floor by
the size of its own cache.

Gemini in Antigravity is the honest gap. Its transcripts hold 9,599 steps
across 89 sessions on this machine and not one token counter, so there is
nothing to bill and nothing to estimate. It is reported by activity, and its
token columns read `no counters` rather than `0`, because a zero in a cost
column says the runtime was free. Antigravity also writes each session twice,
as `transcript.jsonl` and `transcript_full.jsonl` with identical steps, so
reading both would have doubled every count it produced.

Detection is on the shape of a line rather than on the directory it was found
in, so a transcript copied elsewhere still reads correctly and a file in none
of the three shapes is counted as unrecognised instead of being parsed as
though it were.

### What the report does not claim

It cannot price one MCP server. Schema sizes are not in the transcripts, so
inventing a per-server number would mean publishing an estimate next to
measured counts, which is exactly the mistake the efficiency table was built
to avoid. `wn agent tools` reports the floor and the call counts and leaves the
line to the reader.

It does not edit the agent's configuration. Which servers you want loaded is a
judgement about how you work; a tool that quietly disabled one would trade a
token bill for a surprise. The report ends at the names.

A server that shows as idle may simply not have been called in the window
asked for. `--days 7` answers a different question from a full scan, and a
configured name that cannot be matched confidently against an observed one is
listed as idle rather than counted as used. Both defaults point the same way:
raise a question, do not act on it.

### Prior art this was built against

The problem is well documented. Practitioner write-ups put the fixed overhead
of a Claude Code session at 20,000 to 30,000 tokens before the first keystroke,
and a machine with several MCP servers connected at 50,000 to 70,000. The
measured floor here, 53,582, lands inside that range without being tuned to it.

Anthropic's own answer is server-side: a Tool Search Tool that loads a search
shim of roughly 500 tokens and defers the rest, reported at up to 85% of tool
schema tokens preserved, and context editing that clears old tool results
without breaking the prompt cache prefix. A meta-MCP write-up reports the same
idea implemented client-side, 36.6k tokens of tool definitions down to 4.4k.
All of those act inside the request. A `PreToolUse` hook cannot, which settles
what Winnow's contribution here is: the measurement and the recommendation, not
another loading mechanism.

On the reading side, `ccusage` established that the local JSONL transcripts are
a complete usage record and can be parsed offline for per-session and per-model
reports. `wn agent` reads the same files for a different question. `ccusage`
answers what a session cost. This answers which part of the cost was work and
which part was the same prefix being read again.

---

## The store was a gigabyte

`~/.winnow/store.db` held 127 rows and 1.04 GB.

The cap was `max_store_rows`, default 5000, and it had never fired. It counts
rows, and the outputs that fill a disk are not numerous, they are large: one
`rg` sweep over a big tree is a single row holding hundreds of megabytes. 127
rows is 2.5% of the row cap, so by the only measure the store had, it was
nearly empty.

Three changes:

- `max_store_bytes`, default 256 MiB, drops the oldest rows until the stored
  payload fits. Deletion is one row at a time rather than a batch sized from
  the average, because rows differ in size by orders of magnitude and an
  average-sized batch would throw away far more history than the cap asks for.
- `max_row_bytes`, default 4 MiB, caps one stored output. Recall exists so a
  compressed view is never the only copy, and what a reader goes back for is
  the head. The cut is recorded in the stored text, so recall never quietly
  returns a short answer to a long question, and it lands on a character
  boundary so a multi-byte character cannot be sliced in half.
- A prune now vacuums. SQLite keeps deleted pages for reuse, so a store that
  has just dropped 900 MB still reports 900 MB to `du`. The vacuum runs only
  when the free space is worth the rewrite, at least 32 MB or a tenth of the
  file, because VACUUM rewrites the whole database and doing it on every prune
  would be felt on the command being wrapped.

Token counts are not rewritten by any of this. What an output cost the agent is
a property of the output, not of how much of it stayed on disk, so `wn gain`
reports the same figures whether or not a payload was capped. As with every
other setting, 0 turns a cap off, and a value that cannot be read turns off its
own cap and leaves the others working.

---

## Tests

25 new tests: 16 for the transcript readers and 9 for the store caps. The
transcript fixtures are hand-written JSONL in the shape Claude Code writes,
so the suite does not depend on a real transcript existing on the machine
running it. The store tests cover the byte cap, the row cap, the per-output
cap, a cut on a multi-byte boundary, a value that cannot be read, and the
guarantee that a cap never touches the published token counts.
