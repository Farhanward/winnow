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

---

## An end-to-end review of what gets cut

The question this release was reviewed against: can any layer remove something
the model then reasons about wrongly? Compression that loses tokens is the
point. Compression that changes a conclusion is a bug.

Every drop in the pipeline was checked for whether it leaves a mark. Most did
already. `collapse_repeats` writes `⟨×N similar lines⟩`, `cascade_guard` writes
`⟨and N more like this⟩`, `limit_per_file` writes `⟨+N more in <file>⟩`,
`max_line_len` writes `⟨+N chars⟩`, `keep_head_tail` writes `⟨N lines hidden⟩`,
the built-in filters append their own counts, and the footer carries the token
delta and the recall handle. Two places did not.

### The clamps were invisible

`Read` and `Grep` are capped by rewriting the tool input, and the model was
never told. It asked for a file and received 400 lines of it with nothing
saying so. The failure mode is not a missing line, it is a wrong conclusion: a
12,000-line file read to line 400 looks like a file that ends at line 400, and
anything the model concludes about what is not in it is then wrong.

`PreToolUse` hooks can return `additionalContext` alongside `updatedInput`, and
that is what the clamps now do. A clamped `Read` says which lines are being
shown, out of a file of what size, and that an explicit `offset` continues it. A
clamped `Grep` says the limit it was given, that matches past it exist, and that
an explicit `head_limit` overrides it. An unclamped call sends no note, and an
explicit limit from the caller is still never second-guessed.

### JSON elision said nothing

Past the depth limit, `compress_json` replaced a subtree with the bare string
`"…"`. That hid three things at once: how much was dropped, what shape was
dropped, and the fact that anything was dropped at all, since a plain string is
indistinguishable from a value that genuinely is an ellipsis. The result was
still valid JSON, which made it worse: it read as complete.

It now names what it removed, as `⟨winnow: object with 12 keys elided at depth
limit⟩` or `⟨winnow: array of 40 items elided at depth limit⟩`. A scalar at the
depth limit is kept rather than elided, which was the other half of the bug:
replacing the number 42 with an ellipsis costs a reader information and saves
nobody anything.

### What was checked and left alone

- Exit codes survive a wrapped run, and stderr is kept. A command that fails
  still reports the failure and the code it failed with.
- `wn recall` returns the original byte for byte. A 3,000-line output
  compressed to three lines recalls all 3,000.
- An `ERROR:` line buried in 800 lines of pip chatter survives. So does a
  `FAILED` line in a wall of pytest progress dots, and the one distinct error
  at the end of a 30-line cascade of an unrelated one.
- Search results keep every matching file represented. Whether the hits fold by
  fingerprint or by file, each file still appears and each cut is announced.
- `drop_lines` rules do not append a per-rule count, and that is deliberate:
  the footer already reports the token delta and the recall handle, and a
  summary line on each of twenty rules would spend the tokens the rules saved.

---

---

## The input half: reading a file twice

Everything before this compresses what a command returns. The output side is
also where the field already is: `rtk` compresses shell output for sixteen
agents and says plainly that it does not touch input tokens; `lean-ctx` and
`llmtrim` sit in the same lane. The unclaimed half is what the agent asks for.

The measurement first. Across the Claude Code transcripts here, 593 `Read`
calls whose results could be matched to their request carried 24.9 MB into
context. 66 of them asked for the same file, in the same session, over the same
line range, a second time: 2.9 MB, 11.7% of everything `Read` returned, about
725,000 tokens. Every output Winnow has ever compressed for Claude Code saved
97,348. The second copy of a file the model already holds is worth more than
the entire compression side of this tool.

So the hook keeps a ledger of what it has served, keyed on session, absolute
path, offset and limit, with the file's size and modification time as it stood
at the time. A request that matches an entry whose stamp still holds is cut to
a single line, and `additionalContext` says what happened and where the content
the model already has came from.

### Why a stub and not a denial

A `PreToolUse` deny shows `permissionDecisionReason` to the user, not to the
model. A denied re-read would leave the agent with a blocked tool call and no
explanation, which is a good way to make it retry in a loop or invent a reason.
Capping the request to one line goes through the same `updatedInput` path every
other clamp already uses, saves within a rounding error of the same tokens, and
carries an explanation the model can act on.

### Why asking twice always works

The dangerous case is not a wasted read, it is a withheld one. If context has
been compacted, the file the ledger believes the model holds may no longer be
in the window it can see, and suppressing the re-read would leave it reasoning
about a file it cannot look at.

Two things guard that. A suppressed read is marked, and the next identical
request is served in full: if the model insists, it is taken at its word, and
the cost of being wrong is one round trip against a whole file. And
`wn hook install` now registers `PreCompact` and `SessionEnd`, so at the moment
compaction rewrites the window, the ledger forgets that session outright.

The other three refusals to suppress are simpler. A file whose size or
modification time changed is a new read, not a repeat, which is what makes an
`Edit` followed by a `Read` always go through. A different offset or limit is
different content. A different session shares nothing.

### What it costs to be cheap

The fingerprint is size and modification time rather than a content hash,
because this runs on every `Read` the agent makes and hashing a large file on
that path would cost more than the re-read it is saving. The failure mode is a
file edited to exactly the same length within the same nanosecond stamp, which
no editor produces. `dedupe_reads: false` turns the whole thing off.

`wn reads` prints what the ledger believes and `wn reads clear` empties it. A
suppression the user cannot inspect is one they cannot trust.

---

## Tests

52 new tests: 17 for the read ledger, 16 for the transcript readers, 9 for the
store caps, 5 for clamp transparency and 5 for JSON elision. The ledger's cover
the cases where suppression must *not* happen more heavily than the case where
it should, because that is where the cost of a mistake is. The
transcript fixtures are hand-written JSONL in the shape Claude Code writes,
so the suite does not depend on a real transcript existing on the machine
running it. The store tests cover the byte cap, the row cap, the per-output
cap, a cut on a multi-byte boundary, a value that cannot be read, and the
guarantee that a cap never touches the published token counts.
