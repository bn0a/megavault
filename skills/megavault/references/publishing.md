# Publishing

Publishing is projection: no new evidence, claims or resolutions. Anything in the vault that is not in the store is a defect.

| Command | Writes |
|---|---|
| `publish --out <dir>` | a whole new vault into an empty directory (scratch; without `--out`: `$RESEARCH_VAULT_ROOT/<slug>/` or `./vault/<slug>/`, moved to `<run>/vault/<slug>/` when that would land inside a vault root) |
| `publish --brain <root> --vault <name> [--apply]` | a new vault into the brain, with the router row |
| `merge-plan --into <vault>` then `publish --into <vault> --apply` | into a vault that already exists |

Only the third touches live notes (five verbs, merge planner: `expand.md` — read it before any merge). Both brain paths are **dry runs without `--apply`**.

## Layout

```text
<vault>/
  00 <Prefix> Home.md              router; domains, evidence layer, meta
  01 <Domain>/
    01 <Domain> MOC.md             map of content + the questions the domain owns
    <Note>.md                      synthesised notes; dataset notes too
  90 Evidence/
    <Prefix> Source Register.md    <Prefix> Claim Ledger.md (+ `## Claim index`)   <Prefix> Evidence Map.md
    <Prefix> Contradictions.md     <Prefix> Gaps and Backlog.md                    <Prefix> Wanted Notes.md
  99 Meta/
    <Prefix> Metadata Schema.md    <Prefix> Change Log.md                          <Prefix> Validation Report.md
```

Meta files are prefixed from birth (the prefix is the vault's permanent identity in a shared brain; a prefix that cannot be part of a file name exits 2). Content notes stay within 2 hops of Home. Core Obsidian only — wikilinks, frontmatter, tables, callouts; no Dataview, templater or community plugins.

## Note bodies

Synthesist notes (schema: the synthesist definition) are rendered by code: frontmatter → `# Title` → summary → body → `## Claims used` → `## Related` (3–8 links) → `Up:` footer. Rules for any note body:

- **Answer first**; background below.
- Every factual sentence traces to a claim ID, or is labelled analysis, recommendation, or open question; never an inference beside a cited fact. A reader walks sentence → claim → excerpt → source URL without leaving the vault.
- Keep the claim's calibrated wording (`qualified` never becomes "research shows").
- **Wikilink densely on first mention**, whether or not the note exists — a broken link is a work item queued in Wanted Notes.
- State the as-of date for anything versioned or fast-moving.
- Never cite a claim whose status is `unsupported` or `refuted`.

The publisher changes a body only by (`rewrite_claim_tokens`) turning a bare `CL-017` into `[[<Prefix> Claim Ledger#CL-017]]` (IDs the ledger carries; code spans, fences, existing links skipped), (`rewrite_meta_links`) giving a bare meta link the prefix (`[[Claim Ledger#CL-017]]` → `[[<Prefix> Claim Ledger#CL-017]]`, every meta file, alias and anchor kept; code spans, fences and a note that really has that title skipped), and (`add_inline_citations`) appending `([Publisher, date](url))` to a factual paragraph with no URL. Dense linking is the synthesist's job, never a regex's.

Frontmatter is generated. The gate requires `type`, `status`, `evidence_level`, `published`, `last_verified`, `review_due`, plus `claims` (concept/profile/playbook/guide) or `fidelity`, `source_urls`, `as_of` and no `claims` (dataset). `evidence_level` is computed from the claims' tiers (`official` all tier 1, `peer_reviewed` through tier 2, `mixed` if any disputed/refuted, else `empirical`/`hypothesis`) and drives `review_due` — never hand-edit it.

## Wanted Notes

`<Prefix> Wanted Notes.md` lists every unresolved wikilink, grouped by the note that wants it — a work queue: write the note or drop the link, never delete lines by hand. A broken link is a *warning*; one not listed there is an *error*. An older vault without the file shows broken-link errors (hence the merge gate is regression, not zero errors); the first merge creates it.

## Brain publish

`publish --brain <root> --vault <name> [--prefix P] [--when '…'] [--apply]`. Your part:

1. **Identity is permanent**: the prefix is Title-Cased from the kebab name (`pothos-cuttings` → `Pothos Cuttings`). Confirm it with the user and pass `--prefix`. `--vault plants/pothos-cuttings` publishes into an existing group folder (prefix from the last segment).
2. On a needed rename, `link-rewrite` (below) — never a broad regex.
3. Report counts, the open gap list and a summary paragraph; then `validate --brain` over the whole brain and say loudly if anything regressed.

The code (`publish_to_brain`) does the rest: brain root `--brain` → `$RESEARCH_VAULT_ROOT` → `$RESEARCH_BRAIN_ROOT` → `./vault` (a README without `## Router` is refused; an explicit `--brain`, `~` expanded, that is missing or not a vault root exits 2 and never falls back to the others); refuses an existing destination, a no-write area or a first-hand area; builds prefixed in `<brain>/.research-staging/`; `lint-brain` over staging ∪ brain (duplicate basenames block, `README` exempt; `qa/`, `backlog/`, `logs/`, first-hand areas linted, never written); validates staging (errors block; on block or dry run nothing moves); `os.replace`; inserts the router row after the last row of the vault's group section (a `### <group>` heading or a `| **<group>/** |` row) when the router has one, else after the last row of the router table, or prints it for the user to paste. A domain that was deprecated before any work gets no folder; the Gaps and Backlog file lists it under `## Not researched`, and carries a `## Coverage` section when `reports/coverage.json` exists. `router-row --vault <name> [--when …]` previews the row and anchor and stops. When the README already routes that vault (after a merge, say), it never proposes a second row: the output adds `row_exists`, `existing_row`, `current_counts` (sources, claims by status, open gaps, notes, read from the published vault) and `row_update`, the same row with its counts segment (`N sources, N claims (N supported, …), N open gaps, N notes` in the "when" cell) regenerated or appended. Replace that one line with `row_update`; never type the counts.

## Group folders

A **group** (e.g. `<brain>/plants/`) has no vault marker (`00 * Home.md`, `90 Evidence/`, `vault_kind`) itself but has vaults below; groups may nest. Brain-wide commands (`validate --brain`, `lint-brain`, `validate --profile first-hand`, maintenance scripts) expand a group into its vaults. Prefix and Home come from the vault folder's basename; reports, router rows and `--vault` use the brain-relative path — keep basenames unique across groups. Never give a group README a `## Router` heading (`router-in-group`) or put a vault marker in a group folder (`nested-vault`); both fail `lint-brain` and `validate --brain`. `validate <vault>` walks up to the brain; `merge-plan`/`publish --into` accept grouped vaults.

## Renames: link-rewrite only

```shell
python scripts/research.py link-rewrite --map renames.json --root <dir>              # dry run
python scripts/research.py link-rewrite --map renames.json --root <dir> --apply-one <file>
python scripts/research.py link-rewrite --map renames.json --root <dir> --apply
```

A broad regex rewrite can silently corrupt a vault. Matches only `[[old]]`, `[[old|alias]]`, `[[old#anchor]]`, `[[old\|alias]]`. Order: **dry run → read the stderr lines → `--apply-one` one file → grep it → `--apply` → check `reconciled`** (unreconciled exits 1). Never hand-roll `sed`, find-and-replace or a Python one-liner over a vault.

## The gate

```shell
python scripts/research.py validate <vault>
python scripts/research.py validate --brain <root>
python scripts/research.py validate --profile first-hand [--brain <root>]   # first-hand areas, never writes
```

`validate <vault> --brain <root>` checks one vault with links resolved brain-wide (a link into another vault is not broken); `--brain` alone checks every vault, dispatching first-hand areas to their own gate (`field-notes.md`). Exit 1 on failure; errors and warnings go to `99 Meta/<Prefix> Validation Report.md` and `99 Meta/validation.json` (`--no-report` suppresses; the file names the vault by its brain-relative path or folder name, never a local path). Web-derived text cannot pass the gate as structure: an evidence table row with the wrong cell count, no closing pipe or a repeated ID (what a raw newline or pipe leaves, beside a possibly forged row) is an error, and so is executable or remote-embedded content in any research note (Dataview/DataviewJS, Templater or code-runner blocks, `<script>`/`<iframe>`-type HTML, `![](https://…)`). The renderer writes every untrusted value on one line, pipes escaped, tag-like `<` as `&lt;`, quotes with escaped backticks; `publish` checks every path against the target before its first write. A note citing an `unsupported`/`refuted` claim is only a warning: answer it with the NEEDS REWRITE flow (`expand.md`), not by blocking.

Newlines: new files LF, existing files keep theirs (`write_text`/`read_note`).
