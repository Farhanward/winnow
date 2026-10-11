# Winnow report: 11 October 2026

Snapshot: 2026-10-11T03:26:15+03:00. The source was located through the verified migration record. Its Git base matches the fetched GitHub main branch: 4f7f176fe01b45a56b1831e21457be09bfa69eb4. Local changes had not been committed or pushed.

## Savings

The lifetime ledger records **33,151,910 tokens saved** across 9,416 observed outputs, including 720 compressed outputs. Reduction is 92.9% for compressed outputs and 39.8% across all recorded outputs. Counting mode: estimated (heuristic). The most recent recorded lifetime event was 2026-10-11T03:25:33+03:00; this snapshot does not prove collection continued after that event.

The recall store currently holds 806 outputs. Store totals can change with pruning; the lifetime ledger is maintained separately.

## Largest lifetime contributors

| Filter | Outputs | Saved tokens |
| --- | ---: | ---: |
| fold-repeats | 271 | 18,299,185 |
| clip-monster-lines | 212 | 6,919,409 |
| clip-monster-lines+ripgrep | 19 | 5,147,309 |
| clip-monster-lines+fold-repeats | 8 | 2,116,160 |
| clip-monster-lines+fold-repeats+ripgrep | 2 | 308,027 |
| fold-repeats+log-streams | 4 | 80,283 |

## Runtime ledger

| Client | Runs | Input tokens | Output tokens | Saved tokens | Reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| codex | 5,408 | 183,835,596 | 68,986,755 | 114,848,841 | 62.5% |
| claude | 5,049 | 1,695,902 | 1,086,541 | 609,361 | 35.9% |
| gemini | 0 | 0 | 0 | 0 | 0.0% |
| local | 20 | 30,682 | 30,682 | 0 | 0.0% |

These figures come from a separate runtime ledger. Its coverage differs from the lifetime ledger, so the savings must not be added together. The report measures recorded output reduction; it does not measure invoice savings or a controlled speed benchmark.

## Local changes and validation

The unpublished changes add context-pack prompts and extend Codex worker hook handling. Review identified that disabling the budget brief also bypassed the fresh-session default. The correction keeps those controls independent and preserves an explicitly supplied fork setting.

Validation: 163 tests collected, 0 failures, 0 errors, 0 skipped; pytest exit code 0. The JSON commands were run from the migrated source. The installed wn entry point was also checked from outside the repository. Its editable import resolves to that same source. The focused subagent tests passed 18 cases, and review found no remaining defect in the corrected block.

Personal settings under .codex, generated graph files, local databases, temporary test files, and machine-specific AGENTS.md are excluded from publication.

## Tool availability

| State | Evidence |
| --- | --- |
| Unavailable now | claude-mem runtime setup is incomplete. Its local definition also describes a quota-paused worker. |
| Repaired and verified | The installed wn entry point now uses the migrated source on D. Its gain JSON succeeds from outside the repository and agrees with this report. |
| Disabled deliberately | claude-mem mcp-search, headroom, cua_repl, colab-mcp. |
| Requires access outside the sandbox | Git ownership validation, GitHub CLI configuration, and database writes. These restrictions are not proof the tools are broken. |
| Verified | GitHub authentication and fetch; Winnow gain and efficiency JSON; the test suite. |
| Not tested | Every connected app, every skill, and every on-demand MCP server. |

No on-demand MCP session was running at the status check. Compression observed in the current tool transport is not sufficient evidence that the installed local wn entry point works.

## Verification rule

A successful test command must have both its process exit code and a nonzero collected test count checked. A pipeline's final exit status is insufficient evidence for an earlier process.

Source: [pytest exit codes](https://docs.pytest.org/en/stable/reference/exit-codes.html).
