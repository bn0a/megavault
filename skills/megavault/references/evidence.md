# Evidence

One schema, in `scripts/research.py`; if this file and the code disagree, the code is right.

## Mechanical vs judgment

**Machine** (`ingest`/`verify`, enforced whether or not an agent cooperates): source retrieved (cache file exists, else `no_cache`); quote verbatim (normalised substring of the raw cache or of an HTML page's text view = `exact`; identical once punctuation, hyphenation and line numbers are ignored, or window ratio ≥0.92 with the same digits and no flipped negation, direction or quantity word = `fuzzy`; else `mismatch`); locator present (rejected at ingest); independent corroboration (distinct `origin_group` count, else `qualified`).
**Judgment** (verifier on Opus, shown only `(claim, excerpt)`, never the desired conclusion): does the passage *entail* the claim; is the claim's scope no wider than the passage; is the source fit for the claim type (prospector sets tier, verifier challenges). These fail silently — the residual risk.

## Records

Local `key` values are per-file; `ingest` maps them to global IDs (`S-001`, `X-004`, `CL-012`, `EV-031`, `CX-002`, `GAP-003`).

```json
{"key": "s1", "url": "https://…", "title": "…", "publisher": "…", "authors": ["…"],
 "published_at": "2026-03-01 | unknown", "accessed_at": "2026-08-05",
 "source_type": "primary|official|standard|regulation|scholarly|dataset|news|analysis|vendor|community|unknown",
 "authority_tier": 1, "origin_group": "who this ultimately comes from",
 "use": "what this source is good for here", "limitations": "what it cannot establish"}
```

Tiers 1 (primary/official/normative) to 5 (anecdotal): policy only in `sources.md`. **`origin_group` is load-bearing**: if this is wrong, what else is wrong with it? Five outlets reporting one press release share a group; so do a study and its author's blog. The default (registrable domain) is a floor — set the underlying dataset, filing, release or author group whenever known. On an aggregator host (arxiv.org, doi.org, pubmed/ncbi, ssrn.com, semanticscholar.org, researchgate.net, osf.io, biorxiv.org, medrxiv.org) the default is the paper's identifier (`arxiv:<id>`, `doi:<doi>`, `pmid:<n>`, `ssrn:<n>`), never the host.

```json
{"key": "x1", "source_key": "s1", "text": "exact verbatim text — never a paraphrase, never tidied",
 "locator": {"kind": "section|page|paragraph|table|timestamp|anchor", "value": "4.2"},
 "language": "en", "context_before": "", "context_after": ""}
```

Checked against `<run>/cache/raw/<hash>.txt`, which `verify` never changes; normalisation folds only whitespace, Unicode form, quote marks, dashes and case. A cache file that looks like HTML (starts with `<!doctype`/`<html`, or is mostly tags) is also read through a **text view** (scripts and styles dropped, tags stripped, entities decoded, whitespace collapsed): a quote found there is `exact`, a near miss there `fuzzy` by the same ratio. A **loose** form (letters and digits only, so quote marks, dashes, soft hyphens, line-break hyphenation and repeated whitespace vanish; leading manuscript line numbers dropped; decimal points, minus signs, ranges and gaps between digits kept) catches PDF extraction artefacts: a quote of 16+ such characters found only there is `fuzzy` with no score, never `exact`. `verification.basis` says which matched: `raw`, `text_view` or `loose`. A ratio match passes a **meaning guard** too: the quote, lined up word by word with the page passage, must carry the same digit sequence, and no differing word may be a negation (`not`, `no`, `never`, `without`, any `n't`, or `un-`/`in-`/`non-`… of the page's word), a direction (`increased`/`decreased`, `higher`/`lower`, `above`/`below`, `up`/`down`, `positive`/`negative`), a quantity or frequency (`all`, `some`, `only`, `always`, `often`, number words, `about`, `may`/`must`); else it is a `mismatch` and `verification.meaning_change` says why (datacheck cells: the same guard). The window steps by a quarter of the quote's length, so a near miss can score below its best alignment: the check errs towards `mismatch`. It catches formatting differences and those flips, not every change of meaning (an antonym the list does not name passes); whether the passage entails the claim is the verifier's judgment. Ellipses fail — take two excerpts. **The cache holds the page's own text**, fetched raw over `Bash` with `curl -sS -L --proto '=https' --proto-redir '=https' --max-redirs 5 --max-filesize 20000000 --max-time 60 -o '<file>' -- '<url>'` (https and public hosts only; the URL in single quotes, never double quotes; the Python fallback is fixed in the extractor definition); a `WebFetch` result is a summary — fine for navigating, never the cache. `ingest` refuses a source whose URL is not http(s), carries `user:password@`, or names a non-public host (loopback, link-local, private, `localhost`): rule `url:refused` in `reports/rejected.json`, no gap. Failed raw fetch (403, bot wall, JS-only) → reason in the source's `fetch_note`, empty cache, honest `no_cache`.

```json
{"key": "c1", "text": "one atomic, falsifiable proposition",
 "claim_type": "descriptive|numerical|causal|comparative|predictive|normative|platform_statement",
 "importance": "central|major|supporting", "scope": "population, geography, system, conditions",
 "time_scope": "the period asserted", "action": "what a reader should do about it",
 "caveat": "what it must not be read as", "topic_tags": ["…"]}
```

`scope`, `action`, `caveat` turn an assertion into a finding ("up to 40%" needs `scope: lab germination rate, not field yield`). Identical claim text from two workers merges into one record with both edges — so write claims *poolable*: atomic and unhedged, hedging in `scope`/`caveat`.

```json
{"claim_key": "c1", "excerpt_key": "x1", "relation": "supports|refutes|qualifies",
 "strength": "weak|moderate|strong|conclusive", "rationale": "what the passage entails and its limit"}

{"claim_keys": ["c1"], "summary": "…", "axis": "factual|numerical|scope|temporal|definition|method",
 "severity": "low|medium|high", "status": "unresolved"}

{"question": "…", "impact": "critical|material|minor", "next_action": "the best next query"}
```

An edge's `relation` is **claim-relative**: whether *this* passage supports, qualifies or refutes *this* claim's text, not a thesis the passage discusses. A wrong one is corrected only with `relabel-edge` (old value and reason kept on the edge, audit line in `reports/ingest-log.jsonl`), then `verify` (`operations.md`).

A gap also has `status` (`open|closed|superseded`) and **`closed_by`** (the answering claim ID). A worker closes one with `closed_gaps: [{"id": "GAP-004", "closed_by": "CL-031"}]`; `verify` closes any open gap that has `closed_by`. Closed gaps stay published with their closer, so no later run reopens them. A claim may carry **`supersedes`** (another claim's ID): `verify` marks that one `superseded`; it stays in the ledger. Tables (`DT-###`): `data.md`.

Stored by `ingest` beyond the worker fields: claim `arm` (the brief of the prospector arm that first proposed it) with `seen_by_arms` / `seen_by_tasks` when another arm or task re-proposes it; source `seen_urls` (other fetched forms of the same canonical URL); gap `url` (hand-back and policy gaps, one per canonical URL).

## Adopted records

Everything `adopt` creates carries `origin: "adopted"`:

| Record | Semantics |
|---|---|
| Excerpt | `verification.status: "adopted"`; never re-checked against a newer cache; published result kept as `adopted_check` |
| Edge | keeps the vault's decision (`accepted`/`provisional`); not re-adjudicated |
| Claim | keeps `adopted_status`, `adopted_groups`; a `qualified` reason mentioning origin groups → `requires_two_groups: true` |
| Source | tier and group from the Source Register; found only in the Evidence Map → estimated tier, `tier_estimated: true` |

1. **Adopted support is existing support, never a *new* origin group.** A claim rises above its adopted status only when non-adopted evidence brings a group the vault lacked; `verify` reports `origin_groups_adopted` and `origin_groups_new` separately.
2. **An estimated tier drives no ceiling** — excluded from the tier-ceiling computation.

## Status arithmetic (`verify`)

An edge is **accepted** iff its excerpt verified `exact` or `fuzzy`; otherwise **provisional**, supporting nothing. Then: **refuted** = accepted refutation, no accepted support · **disputed** = accepted evidence both ways (never averaged or counted) · **unsupported** = no accepted support · **qualified** = supported, but a `central` claim or one typed `numerical`, `causal`, `comparative` or `predictive` has fewer than **two distinct origin groups** · **supported** otherwise. `qualified` is the most common honest outcome and publishes.

A primary source alone establishes what it *says, does, or configures* — never the external effect of its own self-interested claim: type that `platform_statement` and keep the effect as a separate claim needing independent support.

## Published tables (v2 headers)

| Table | Columns |
|---|---|
| Claim Ledger | ID · Claim · **Type** · **Importance** · Status · Evidence · Scope, action, caveat · Groups |
| Source Register | ID · Tier · Accessed · Source · **Origin group** · Use · Limitations |
| Gaps and Backlog | ID · Open question · Impact · **Status** · **Closed by** · Next action |
| Contradictions | ID · Claims · Axis · Severity · Status · Summary |

Below the ledger table, `## Claim index` holds one `### CL-###` heading per claim (text + Evidence Map pointer, no status) so `[[<Prefix> Claim Ledger#CL-###]]` lands on the claim; merges only append to it. Bold columns do not exist in v1 vaults → `manual[]` (`expand.md`).
