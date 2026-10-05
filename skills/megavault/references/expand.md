# Expand

Research *over* an existing vault — close its gaps, lift qualified claims, settle disputes — and merge back without overwriting a line. `adopt` brings the vault in; the five write verbs put the run back; between them is the normal loop.

## 1. Adopt

```shell
python scripts/research.py adopt <vault> [--brain DIR] [--prefix "Mini"] [--profile P] [--force]
```

Creates or reopens run `expand-<vault-slug>-<hash>` (mode `expand`), writing `<run>/vault_profile.json` and `<run>/reports/adopt.json`. Re-adopting needs `--force`, which rewrites the adopted records. An evidence table with a malformed row (wrong cell count, no closing pipe, a repeated ID) STOPs adopt with exit 3 before anything is written: a forged row may sit beside it, so a human repairs it first. A source URL with `user:password@` is adopted without the credential.

- **Prefix** (`Mini` → `Mini Claim Ledger`, `00 Mini Home`) is inferred from the `90 Evidence/` basenames and the Home name (`confirmed` / `inferred` / `declared`). When it cannot be inferred unambiguously `adopt` STOPs with exit 3 — a question for the user, never a guess. A prefix derived from a vault name (`pothos-cuttings` → `Pothos Cuttings`) must be confirmed with the user and declared with `--prefix`.
- Parsed: the `CL-###`, `GAP-###`, `S-###` tables, Evidence Map blocks and note frontmatter — never note prose. Domain folders become `D1…Dn` with their MOC description and `## Questions this domain owns`.
- **ID high-water**: counters continue from the vault's highest IDs. Read `next_ids` from `adopt.json` aloud once — silent ID collision is what this prevents.
- Adopted records (`origin: "adopted"`) follow `evidence.md` § Adopted records: existing support, never a *new* origin group.

## 2. The pick-list

`status --expand` ranks the open surface: (1) open critical gaps, (2) open material gaps, (3) disputed claims, (4) qualified claims that are `central` or require two groups, (5) unsupported claims with no edge and no citing note, (6) MOC questions in domains without notes. Minor and closed gaps are omitted. Each item carries `next_action_from_vault`, passed to the worker verbatim. Propose a pick-list from it; the user's approval is the gate.

## 3. Targeted packets

```shell
python scripts/research.py packet --expand --pick GAP-003,CL-017 [--domain D2] [--brief blind --brief-text '…'] --role prospector
```

Item-scoped; an unknown pick is a hard error, and a bare domain id (`--pick D2`) picks that domain's open gaps, claims and MOC questions. Fields: `domain_id` (from `--domain`; workers copy it into their result so notes land in the right folder); `targets[]` (per pick: `objective`, `kind`, `impact` or `current_status`, `status_reason`, `next_action_from_vault`); `objective` (a gap's question verbatim, or generated from the claim's status); `exclude_origin_groups` (groups already behind the picked claims — corroboration from them does **not** count); `already_seen_urls` (≤500); `linkable_titles` (≤300 existing basenames, for dense linking); `vault_prefix`; `budget`; `source_policy`; `not_found_is_valid: true`; `inbox`; `cache_dir`.

## 4. The five write verbs

Enforced by `apply_op`: paths may not escape the vault; every anchor must match exactly once.

| Verb | Precondition | Used for |
|---|---|---|
| `create` | file absent **and** basename unique brain-wide | new notes, MOCs, a first Wanted Notes |
| `append-row` | anchor matches once (no anchor → end) | ledger/register/gap/contradiction rows, MOC bullets, `claims:` entries |
| `patch-cell` | `old_line` matches one line (logged in the plan) | status flip, `last_verified` bump (notes and Home), Evidence Map status line, the ledger's old refutation-rule sentence (once, in a vault published before it was corrected) |
| `append-section` | `insert_after` matches once | `## Update <date>` on a note, new `### CL-###` evidence block, new edges, Home `run_ids`, the run's policy exceptions under the Source Register (`## Policy`) |
| `append-entry` | none | one Change Log entry per publish |

**Forbidden, no override:** deleting, renaming, rewriting a body, regexing prose, editing hand-written notes. A published note is never re-authored: it gets an Update section, its `claims:` list extended line-exactly, its `last_verified` patched.

**The Home records every run.** A merge patches the Home's `last_verified` and lists the run in its frontmatter `run_ids` (created after `run_id` on the second run, then one `append-row` per later run); `run_id` stays the run that first published the vault. The Change Log entry's heading names the run.

**When status invalidates prose** (`supported → refuted` or `→ disputed`): the planner adds the Update section and a Change Log **NEEDS REWRITE** line (note, claim, transition). Tell the user; a human decides what the note should say.

**Hard stops** (exit 2, nothing written): brain-wide basename collision for a `create`; no inferable prefix or unprefixed files in `90 Evidence`/`99 Meta`; an existing ID with different claim text, source URL or gap question; a target that is not a vault of the brain (directly or in a group); a path escaping the vault; a no-write target (`qa/`, `backlog/`, `logs/`, `design/`, first-hand areas).

`manual[]` collects what the verbs cannot express — mostly an older table missing a newer column (`Origin group`, `Type`, `Importance`, `Closed by`). A human pastes those values after extending the header by hand; header rewrites are not a verb. Losslessness is forward-looking.

## 5. Order of operations

```shell
# 1. dry run — always first
python scripts/research.py merge-plan --run <id> --into <vault> [--brain DIR | --no-brain]
# 2. read the table on stderr, show the user, get approval
# 3. apply the reviewed plan
python scripts/research.py publish --run <id> --into <vault> --apply
```

`merge-plan` writes `<run>/reports/merge-plan.json`, the reviewable table to **stderr**, the summary to **stdout**; exit 0 (`applied: false`) when clean, 2 on hard stops. `publish --into` without `--apply` equals `merge-plan`; with `--apply` it replays the **stored** plan (hand edits included). Apply refuses if any file the plan read has changed — **never apply a plan you have not just regenerated.**

Apply is atomic (`apply_merge`): staged copy (`.git`, `.obsidian` excluded), ops, validation against the vault's `baseline_errors` (only *new* errors abort, so an older vault still accepts a clean merge), per-file `os.replace` with backups, full restore on any failure. Afterwards `validate` reports `errors_before_merge` and `regressed`; a regression exits 1 — say so loudly.

Sequence: `adopt` → `status --expand` → approved pick-list → `packet --expand --pick …` → prospector, extractors → `ingest` → `verify` → `status` (`resume` proposes a synthesist for every domain whose claims gained evidence) → synthesist (with `linkable_titles` from `vault_profile.json`) → `ingest` → `merge-plan` → approval → `publish --into … --apply` → `validate`.

Sanity check: one new origin group behind one claim yields exactly **one** `patch-cell` changing one ledger line (besides the Home's `last_verified`, and once the old refutation-rule sentence of an older ledger). A large diff for a small finding is a bug symptom — stop and read the plan.
