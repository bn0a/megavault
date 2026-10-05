# First-hand knowledge (field notes)

The user's own measurements, lessons, negative results, tool traps and working procedures. The schema is enforced by `validate --profile first-hand` and summarised below; this file is the pipeline's view.

A pipeline claim needs a cached quote from an http(s) source, which first-hand knowledge never has. Design rule: first-hand knowledge enriches topic vaults through links and callouts, never through a claim ledger, claim status or source register.

## Areas

- `field-notes/` is first-hand by name; any other vault folder (top-level or in a group such as `notes/`) is first-hand when its Home (`00 <Name> Home.md`) frontmatter has `vault_kind: first-hand`.
- Layout: one root Home; MOCs `<Name> - <Topic> MOC` in numbered folders (Home may set `moc_prefix:`); optional `99 Meta/` files prefixed `<Name> `.
- **No-write for this tool**: `merge-plan`, `publish --into` and `publish --vault` refuse them.

## Types (validated by type, wherever the note lives)

| Type | Page | Lessons |
|---|---|---|
| `case-study` | `## What we set out to learn`, `## Setup and conditions`, `## What we measured`, `## Lessons`, `## What stayed open`, `## Related` | `### Lesson - …` (may have none) |
| `practice-guide` | `## When to use this`, `## Procedure`, `## Traps`, `## Related` | `### Lesson - …` under Procedure, `### Trap - …` under Traps |
| `field-note` | older one-lesson note, still valid | the note itself |
| `home`, `moc`, `meta` | structural files | — |

A research vault may hold a first-hand page if it has a first-hand `type`, is routed from that vault's MOC and Home, and has no `claims:`. It is an ERROR for it to carry `claims:` or be cited in a Claim Ledger / Source Register row.

## The gate

```shell
python scripts/research.py validate --profile first-hand [--brain <root>]      # every first-hand area
python scripts/research.py validate --profile first-hand <area-dir> [--brain <root>]
python scripts/research.py validate <area-dir>                                  # dispatches automatically
```

Read-only (alias `field`); `validate --brain` runs it for every area and checks callouts brain-wide. It enforces the schema note: page frontmatter (`type, status, project, period, topics, bears_on, artifacts, origin, published, last_verified, tags`; `superseded` needs `superseded_by`; `artifacts` non-empty; `bears_on` = existing basenames), required headings, each lesson's two-column table (`Kind`, `Scope`, `Evidence (number / generalization)`, `Sample`, `Invalidated by`, `Supersedes`, `Bears on`), lesson reachability from a MOC row and a Home, brain-wide link and `#heading` resolution, and callouts — each `> [!example] Tested first-hand` ends with `Details and data: [[<Page>#Lesson - …]]` landing on a Lesson/Trap section. Write headings `Lesson - <title>` / `Trap - <title>` (no `# | ^ : % [ ]`; a renamed lesson fails until every link is rewritten). Warnings include pages over 2,800 words and non-English prose (stop-word heuristic) outside code/blockquotes. Filenames plain ASCII English, basenames unique brain-wide.

## What the pipeline may and may not do

- A lesson may be handed to a **prospector in an `expand` run as a hint**: paste its claim, the page's setup and the lesson's `Invalidated by`, labelled as a hint.
- A lesson is **never a Source**: never in the Source Register, never an excerpt, never an origin group, never a reason to raise a claim's status.
- Research notes link to lessons in prose or via callouts; lessons link to research notes freely.
