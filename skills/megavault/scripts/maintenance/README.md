# Maintenance scripts

Read-only unless the table says otherwise. Run with the brain root as the first argument. Results are per vault:
a top-level folder, or a vault inside a group folder (`plants/<vault>`, see `references/publishing.md`).

| Script | What it does | Writes? |
|---|---|---|
| `retrofit_claim_index.py` | Adds the `## Claim index` section (one `### CL-###` heading per claim) under every Claim Ledger table so `[[<Prefix> Claim Ledger#CL-###]]` links resolve. Idempotent, byte-preserving. `--help` for usage. | Yes, with `--apply`: ledgers in the given brain (dry run by default) |
| `check_anchors.py` | Counts unresolved `#CL` anchors per vault (simple checker). | No |
| `anchor_audit.py <brain> <out.json>` | Independent anchor checker (fence-aware, case-insensitive headings, block ids). | Only the JSON report |
| `content_stats.py` | Per vault: claims without a source, source entries without a URL, non-English text (stop-word heuristic) in agent-readable notes. | Only the JSON report, with `--json <file>` |
| `run_stats.py [--runs-root DIR] [--json]` | Not a brain script: aggregate counts over every run in the runs root (runs, kinds, modes, domains per run, sources, claims, notes, claim status, quote-check results, hand-back status). Never prints run ids, questions, URLs or paths; refuses to print (exit 3) if one would leak. Source of the aggregate numbers in the repository README. | No |
| `resume_all.py [--runs-root DIR] [--json]` | Not a brain script: lists every open `/megavault` run from `~/.claude/research-runs/ACTIVE.json` (status, last and next command, `/megavault resume <id>`), plus runs that predate the file. | No |

**Durability limit of the run store:** `write_json` is atomic (temp file + replace) but never fsyncs. A power cut mid-write can lose the last write; roll-forward repairs a pending ingest commit (`commit.pending.json`), not a file the OS had not flushed.

Tests for the first-hand validation (field-note, case-study, practice-guide, areas, mixed vaults, callouts): `tests/test_field_profile.py`. Grouped vaults in these scripts: `tests/run_offline.py` test_30.

The underlying rule: every claim must carry a resolvable `#CL` anchor back to its ledger entry,
so a retrofit that adds anchors is run once and then checked by the anchor audit rather than
repeated by hand.