# Taxonomy

Everything downstream — worker assignments, folders, MOCs, coverage — derives from the taxonomy; good research cannot fix a bad one.

## Recon first, always

Never propose domains from prior knowledge (stale, generic, the field's marketing categories). Before drafting:

1. Broad searches in the user's framing, then in the field's own vocabulary.
2. Open the 5–10 most authoritative results; identify the primary sources — standards bodies, regulators, first-party docs, the papers everyone cites.
3. Note the field's **disagreements** — that is usually where a domain boundary belongs.
4. Note what is **time-sensitive** (versioned, deprecated, dated) — it needs freshness treatment, often its own domain.

Recon output is not evidence and is never ingested.

## Cutting domains

Ids are 1–32 letters, digits, `_` or `-` (they become task ids and packet file names); a name becomes the folder `NN <name>`, so `taxonomy --file` refuses `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|` and a trailing dot or space (exit 2).

**4–12 domains.** A domain is well-cut when it can be researched **independently**, its questions share an **evidence profile** (don't fuse first-party-doc questions with empirical-study ones), a reader would look for it in one place, and it owns 2–8 concrete questions.

| Cut by | Use when |
|---|---|
| Foundations vs. application | almost always — definitions are stable, tactics are not |
| Actor or platform | distinct implementations with distinct docs |
| Technical vs. editorial vs. governance | different evidence sources, different owners |
| Measurement | always separate: how you know ≠ what is true |
| Risk, failure, and abuse | separate, or it becomes an afterthought |
| Practitioner playbooks | separate; synthesis of the others, produced last |

Bad cuts: chronology; beginner/advanced; the shape of your search results; two domains citing the same sources for the same claims.

Always include a domain owning **measurement/evidence quality** and one owning **limits, risks and open questions**.

## The file

```json
{
  "domains": [
    {
      "id": "D1",
      "name": "Foundations",
      "description": "Definitions, mechanisms, and what the terms actually denote.",
      "kind": "research",
      "questions": [
        "How is X defined by its primary sources, and where do definitions conflict?",
        "What is the mechanism, as described by first-party documentation?"
      ]
    }
  ]
}
```

`kind` (`research`, `data`, `mixed`) defaults to the run's kind — write it only for a domain that differs. A `data` domain yields `DT-###` tables and dataset notes (`data.md`). `id` must stay stable: it keys every claim's `domain_ids` and the published folder; renaming is fine, changing an `id` orphans records. Folder numbers (`01 Foundations`) come from list order — order it as a reader should meet it.

## The approval gate

**Show the taxonomy before dispatching a single worker** as a compact list: name, one-line description, question count. Ask directly whether anything is missing, mis-cut, or out of scope. After this, every mistake costs a wave. Treat a rejection as information about the field: if a domain is missing, recon missed something and neighbouring domains may be wrong too.

## Revising mid-run

Adding a domain in wave 2 is legitimate when a gap pass reveals a missing area: set its `status` to `pending`, add it to the file, re-run `taxonomy --file`, and tell the user the budget implication. Never silently retire a domain that produced claims — mark it `deprecated` in the file.
