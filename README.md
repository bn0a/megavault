# megavault

Research notes from Claude Code, with each claim's status computed from quotes checked against
cached copies of the pages they came from.

## What it is

`megavault` is a Claude Code plugin that takes a research question and turns it into an
Obsidian-style Markdown vault. Subagents search the web, pick the sources worth reading, and cache
the text of each page they take evidence from. Claims are linked to verbatim quotes, each quote is
checked against the cached copy of its page, and a claim with no accepted quote is kept and marked
`unsupported`.

A claim's status (`supported`, `qualified`, `disputed`, `unsupported`, `refuted`, or `superseded`
when a later claim replaces it) is computed by a deterministic script; no model asserts it. The
script counts the quotes that passed the check and the distinct origin groups behind them, and
applies the tier cap of the source profile in use. Model judgments still feed it: whether a passage
supports or refutes the claim, which sources share an origin, each source's tier, each claim's type
and importance (which decide whether it needs two origin groups), and which claim supersedes
another. The subagent that fetched a page also wrote its cached copy. So a status tells you how
well a claim is backed by checked quotes, not whether the claim is true.

An example from the small houseplant vault the offline tests use (`tests/fixtures/mockbrain/mini/`).
The claim below is `qualified`: its one quote passed the check, but a numerical claim needs two
independent origin groups to be `supported`, and this one has a single lab study behind it.

| | |
|---|---|
| Claim `CL-002` | Setting the grow light two steps brighter increases leaf growth by roughly a third. |
| Status | `qualified` (1 origin group) |
| Quote | "Leaf growth rose by 34 percent when the light was set two steps brighter." (check: `exact`) |
| Caveat | One lab study, 42 cuttings. Do not quote the effect size as settled. |

## What you get

A vault published as `houseplants` (a fresh run names its folder and prefix after the question):

```text
houseplants/
  00 Houseplants Home.md          entry page: links every domain and the evidence layer
  01 Growing Variables/           one numbered folder per domain
    01 Growing Variables MOC.md   map of content for the domain
    Light Level.md                a note; each claim ID in it links to the Claim Ledger
  90 Evidence/                    Claim Ledger, Source Register, Evidence Map,
                                  Contradictions, Gaps and Backlog, Wanted Notes
  99 Meta/                        Validation Report, Change Log, Metadata Schema
```

A reader starts at the Home page, opens a domain's map of content, then a note. Each claim ID in a
note links to that claim in the Claim Ledger (status, scope and caveat), and from there the Evidence
Map shows the quotes behind it with the result of each check.

## How it works

One coordinator skill runs inside your Claude Code session and hands bounded jobs to four
subagents:

- a **prospector** picks which sources in a domain are worth reading;
- an **extractor** fetches and caches those pages and returns verbatim quotes;
- a **verifier** judges, for disputed, qualified and central claims, whether the quotes really
  entail the claim and whether its scope exceeds them;
- a **synthesist** writes notes from the accepted claims.

Opus runs the judgment roles and Sonnet the extractor. One deterministic Python engine,
`research.py`, owns every record. Workers are told to write only result files and cached pages
(nothing confines them; see [Security](#security)). The engine ingests those files, checks each
quote against its cache, computes every status and publishes the vault behind a validation gate.
It refuses an Opus packet past the wave's Opus cap, and any packet past the token ceiling unless
forced (logged as a deviation); sources and waves are reported against their ceilings, not
enforced. The verifier's rulings never change a status on their own.

A run goes plan (a taxonomy of 4 to 12 domains that you approve before any worker is dispatched)
→ waves of prospectors and extractors → verify, whose open gaps feed the next wave → synthesise →
publish. The run store under `~/.claude/research-runs/` is the system of record, so an interrupted
run continues with `resume`.

## Measured on real runs

Aggregate counts over 24 research runs by the author; the runs, their topics and their notes are not published, and `skills/megavault/scripts/maintenance/run_stats.py` recomputes the same counts on your own runs.

| | |
|---|---|
| Source records across runs | 727 (546 fetched during a run, 101 adopted from an existing vault, 80 from early runs before origin was recorded) |
| Claims with a computed status | 2,901 |
| Quote records | 4,915 (4,653 checked against a cached page; 247 adopted from an existing vault and 15 without a cached page were not re-checked) |
| Notes written | 469 |

- **Claim status:** 35.6% supported, 39.3% qualified, 18.2% unsupported (plus 6.5% superseded by a later claim, 0.2% disputed, 0.1% refuted). An `unsupported` claim has no accepted quote behind it. This is not an accuracy rate: a status says how well a claim is backed by checked quotes, not whether the claim is true.
- **Quote check:** of all quote records, 4.7% did not match their cached page and were not accepted as evidence; 3.2% matched only after normalising punctuation, hyphenation and whitespace, or approximately (similarity at or above 0.92), in both cases with identical numbers and no flipped negation or direction word, and were accepted as `fuzzy` (that is the current rule; the earlier runs in this sample used a looser fuzzy rule without the meaning check); 0.3% had no cached page. In the runs where mismatches were inspected, most were formatting differences (table pipes, broken characters, PDF hyphenation and line numbers) rather than paraphrases, so the check errs on the strict side.
- **Reliability:** after crash-safe ingest and `resume` were added, three interrupted runs (each stopped twice) all finished from their run store. Three runs: an early result, not a rate.
- **Engineering:** 169 offline tests run without network or model calls; the instructions loaded on every call fit in about 7 KB.

Not measured: accuracy against a human audit, a baseline against an ungated pipeline, or repeat runs of the same question. Token figures are approximate.

## Install and first run

You need Claude Code and Python 3.10 or newer (standard library only, nothing to `pip install`);
discovery uses Claude Code's own web tools, so there are no API keys. The full requirements are in
[USERMANUAL.md](USERMANUAL.md#requirements). Install the plugin from GitHub, from your shell:

```sh
claude plugin marketplace add bn0a/megavault
claude plugin install megavault@megavault
```

A coding agent can run these commands for you: paste the block to it. Then start Claude Code and ask
your question:

```text
/megavault plan <question>
```

`plan` proposes a taxonomy of domains and stops for your approval; `/megavault run` then does the
research. Runs are token-heavy: in the few runs where tokens were recorded, 5 to 10 domains used
roughly 1.4 to 7 million subagent tokens each, so add `--mode small` to `plan` for a first, smaller
run.

The output is plain Markdown with wikilinks, and Obsidian is the recommended way to read it. For a
fresh vault, do nothing: by default a run ends by writing a new vault folder under `./vault/` in the
working directory, which you open in Obsidian as a vault of its own. To grow a vault you already
keep, make it a git repository whose root `README.md` has a `## Router` section (the
[Quickstart](USERMANUAL.md#quickstart) shows how). Then publish each topic into it with
`/megavault publish --brain <vault-root> --vault <name>`; it is a dry run until you add `--apply`.
The coordinator is told to stop if the folder has no `.git`, and after publishing to commit only
the new topic folder and `README.md`.

npm package: coming soon.

## Security

- Subagents read untrusted web pages, and all four hold `Bash` (with `Read`, `Write`, `Glob` and
  `Grep`). The prospector, extractor and verifier also hold `WebFetch` (the prospector and verifier
  `WebSearch` too); the synthesist holds no web tool. Instructions inside a page are treated as
  data, but that is a model behaviour, not a guarantee.
- Whatever the model does, the engine never lets a URL with `user:password@` or a non-public host
  (loopback, link-local, private, `localhost`) become a source, cache key, gap or policy URL (free
  text such as a verbatim quote is stored as written). It writes web text on one line with pipes
  escaped so it cannot forge a row or a status, and fails `validate` on a note with Dataview,
  Templater, script or remote-embed content.
- Extractors and verifiers fetch raw pages themselves through Bash (with `curl`, or the extractor's
  Python fallback), so WebFetch permission rules do not cover those fetches. The prescribed `curl`
  line allows https only, on redirects too, with size and redirect limits. The Python fallback
  checks only that the first URL is https. Neither re-checks the host a redirect leads to.
- Run with permission prompts on (not `--dangerously-skip-permissions`, and no blanket `Bash(*)`,
  `Bash(curl:*)` or `Bash(python:*)` allow rules), and prefer a sandbox or container for long
  unattended runs.
- Vault content is web-derived: review it before you share it. A source title can still carry its
  own Markdown link, and a claim text can render as a heading or callout in the Claim index;
  neither changes a row or a status.

## More

- [USERMANUAL.md](USERMANUAL.md): quickstart, every `/megavault` mode, environment variables,
  troubleshooting, the npx alternative, and the known limits.
- [AGENTS.md](AGENTS.md): running the tests, contributing, and the invariants a change must keep.
- [SECURITY.md](SECURITY.md): how to report a vulnerability.
- [LICENSE](LICENSE): MIT.
