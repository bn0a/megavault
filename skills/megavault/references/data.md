# Data mode

For questions answered by a **table** (e.g. every grow light's spec sheet), each value traceable to the page it was read from.

## When it applies

- `init --kind data` (or `/megavault extract <q> --into <vault>`, sugar for it);
- `init --kind mixed`, when some domains are prose and some tables;
- a single taxonomy domain with `"kind": "data"`, in any run.

Domains inherit the run's `kind` unless they declare their own. Waves, packets, budgets, source policy and the publish gate are unchanged.

## The record

`records/tables.json`, IDs `DT-###`, one record per table, returned by workers under `tables`:

```json
{
  "title": "Grow Light Specs",
  "columns": ["Model", "Watts", "Coverage cm", "Price"],
  "as_of": "2026-07-14",
  "locator": "2026 catalogue, table 1",
  "source_key": "s1",
  "rows": [
    {"cells": ["Model A", "45", "120", "$180"], "fidelity": "verbatim",
     "source_key": "s1", "locator": "table 1", "as_of": "2026-07-14"}
  ]
}
```

`ingest` rejects the file (fatal) unless every row has as many cells as columns, resolves a `source_key` (its own or the table's), and has a valid `fidelity`. Put the **entity** in the first column — disagreement detection and wikilinking key off it.

## Fidelity

| Level | Contract | Enforced by |
|---|---|---|
| `verbatim` | the cell text appears in the cached fetch of its own source | `datacheck`, per cell |
| `derived` | carries a `formula` **and** a non-empty `inputs` list of the verbatim values it was computed from | `ingest`, fatal |
| `observed` | carries an `evidence_note` **and** an `artifact` path existing under `<run>/cache/raw/` | `ingest`, fatal |

An `observed` value is measured (screenshot, timing, log), never presented as a first-party specification.

## datacheck

```shell
python scripts/research.py datacheck [--run <id>]
```

Cell-level over `records/tables.json`: `exact` · `fuzzy` (≥0.92, same digits, no flipped word: the meaning guard in `evidence.md`) · `mismatch` · `no_cache`; a row takes its worst cell; exits 1 on any `mismatch`. Writes `reports/datacheck.json` and stamps rows. Mismatch rows publish with a ⚠ marker and are named in the dataset note's Gaps — never silently dropped, never trusted.

## Disagreement

Same entity, same column, different value, genuinely different sources → **both rows stay** and `ingest` opens a contradiction. Values are never averaged, reconciled, or resolved by counting; a wrong source is a finding to establish with evidence.

## The dataset note

Rendered by code: `type: dataset` inside the owning domain folder (not `90 Evidence/`); no `claims:`, but `fidelity`, `source_urls`, `as_of`; extra `Fidelity`/`Source`/`As of` columns; first-column entities wikilinked whether or not a note exists (queued in Wanted Notes); `## Derived values`, `## Observed values`, `## Design reading`, `## Gaps`.

## Don't adjudicate a spec table

A verifier asking "is a 45 W rating *correct*?" has left the evidence layer. Record what each source says, attributed and dated; verify the transcription (`datacheck`) — that is the whole quality claim; publish conflicts. If a number's *meaning* is contested ("measured at the wall socket or at the LED board?"), that is a **claim** in the normal ledger, not a cell.
