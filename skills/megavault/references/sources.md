# Sources

Source quality is a **selectable, topic-independent profile**, written by source *class*, never by site list.

## Tier doctrine

Every source gets tier 1–5 at ingest. Tier is **what a source can establish**, not prestige.

| Tier | Class | Establishes |
|---:|---|---|
| 1 | primary, official, normative — first-party docs, specs, standards, regulation, filings | what the thing *is*, says, does, or is configured to do |
| 2 | peer-reviewed | a measured effect, within the paper's own scope |
| 3 | transparent preprint or open artifact — data, code, reproducible method | the same, at lower assurance |
| 4 | commercial or observational — vendor studies, industry reports, journalism | direction and rough magnitude |
| 5 | anecdote — forums, comments, uncredited posts | that someone believes or experienced it |

A vendor's docs are tier 1 for *what the vendor does*, tier 4 for *whether it works*. A primary source never alone establishes the external effect of its own self-interested claim (`platform_statement`; `evidence.md`).

**Tier sets a status ceiling, not admission.** Under the default profile a claim resting only on tier-5 support stays at most `qualified`, however many agree. A guessed tier (adopted, `tier_estimated`) is excluded from the ceiling.

## Profiles

`scripts/source_profiles.json` — user-editable data. A profile never decides truth; it decides which sources may enter and how high a claim on a given tier may be graded.

| Profile | Admits | Ceiling |
|---|---|---|
| `open` | everything | tier 5 → `qualified` |
| `strict-academic` | primary, official, standard, regulation, scholarly, dataset only; forums, blogs, wikis, social, vendor marketing rejected | tiers 4 and 5 → `qualified` |
| `practitioner` *(default)* | strict-academic plus signed practitioner material — conference talks, engineering write-ups, named-author technical writing; anonymous forums still rejected | tier 5 → `qualified` |
| `sentiment` | everything; forum, review and comment material is the **primary artifact** | none — tier-5 text is exactly what is being measured |

**Insider sources by default.** Insider = conference talks, named practitioners writing from their own work, official docs, peer-reviewed or benchmark papers. Rejected even when on topic: undated listicles, affiliate/SEO blogs, AI-generated aggregators, and a secondary summary when its primary exists. Code enforces types (`community`, `unknown`) and domain lists; the rest is the prospector's judgment. A field-specific allow-list is a profile you add to the JSON yourself (`allow_domain_suffixes`); none ships. Another profile: `init --profile open`, or mid-run `policy --profile <name>`.

Rule keys: `allow_source_types` (null = any), `deny_source_types`, `allow_domain_suffixes`, `deny_domain_suffixes`, `max_authority_tier`, `tier_status_ceiling`. Evaluation order: run exceptions → allow-suffixes → deny-suffixes → allowed types → denied types → tier ceiling.

Select with `init --profile <name>`, switch a live run with `policy --profile <name>`. An exception with `--scope D5` applies to that domain only, otherwise run-wide (e.g. `strict-academic` overall, forums admitted for D5).

## Enforcement and exceptions

`ingest` enforces mechanically: a rejected URL is dropped with its excerpts and edges, logged to `reports/rejected.json`, and turned into a GAP naming the `policy --allow` command that would admit it, host shell-quoted (one gap per canonical URL, however many workers hit it). `open` rejects nothing. Under every profile, a URL that is not http(s), carries `user:password@`, or names a non-public host (loopback, link-local, private, `localhost`) is refused the same way (rule `url:refused`, credentials stripped from the log) but opens no gap, and a hand-back URL like that is listed under `refused_urls`, never fetched or published. The host is the URL's host name: a port or credentials never change which rule matches. **An agent does not argue with a rejection** — report it; the user decides.

```shell
python scripts/research.py policy --allow reddit.com --reason 'the dispute only exists here' --scope D5
#   → prints the proposed exception, applies nothing, exits 3
python scripts/research.py policy --allow reddit.com --reason '…' --scope D5 --confirmed
#   → written to manifest.policy.exceptions
```

`--reason` is mandatory and published; free text goes in single quotes. The domain is a bare public host name or suffix (no scheme, port, path or `@`; never an IP address or `localhost`), else exit 2. Exit 3 without `--confirmed` is the user's checkpoint, not an error to route around. `--deny <domain>` narrows the same way. Every exception is published in the Source Register's **Policy** section (action, host, scope, profile, date, reason), under the profile the run used; a merge appends the run's exceptions the register does not list yet.

## Search discipline

`searches_per_agent`, `fetches_per_agent`, `read_tokens_per_agent` (in every packet) are ceilings, not targets; on hitting one, stop and hand back (`operations.md`). A prospector shortlist never exceeds `fanout`; an extractor gets exactly one URL.

## "Not found" is valid

Every packet carries `not_found_is_valid: true`. An agent that searched properly and found nothing returns an **empty result plus its search log** — ingestable, it becomes a GAP. Never downgrade to the weakest available source to avoid an empty hand.
