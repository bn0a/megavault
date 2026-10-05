# AGENTS.md

Guidance for coding agents (and humans) changing this repository. For what the product does, read `README.md`. For how the skill behaves at runtime, read `skills/megavault/SKILL.md`; that file is the coordinator's protocol, not documentation about the code.

## Repo map

| Path | Purpose |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest (the repository root is the plugin root). `name` namespaces every component: the skill is `/megavault:megavault` (short form `/megavault`), the agents `megavault:research-<role>`. `version` pins what users get. |
| `.claude-plugin/marketplace.json` | Makes this repository its own marketplace (`claude plugin marketplace add bn0a/megavault`); one entry, `source: "./"`. |
| `skills/megavault/` | The skill directory, installed as a whole by every install path (plugin, npm, manual). Everything the skill references lives inside it, so `${CLAUDE_SKILL_DIR}` resolves the same way everywhere. |
| `skills/megavault/SKILL.md` | Coordinator protocol loaded by Claude Code: invariants, modes, loop, when to ask the user. |
| `skills/megavault/scripts/research.py` | The engine. Run store, ingest, quote verification, status computation, packets, budgets, merge plans, publish, validation gate. Single file. |
| `skills/megavault/scripts/source_profiles.json` | Source-quality profiles as data. |
| `skills/megavault/scripts/maintenance/` | Read-only audits over published vaults and the runs root, plus `run_stats.py` (aggregate, privacy-safe run statistics). `retrofit_claim_index.py` is the one that writes. They find `research.py` relative to themselves. |
| `skills/megavault/references/*.md` | Progressive-disclosure docs the coordinator opens on demand (taxonomy, operations, evidence, sources, expand, data, publishing, field-notes). |
| `agents/research-*.md` | The four subagent definitions (prospector, extractor, verifier, synthesist): role, return JSON shape, hand-back, hard rules. Plugin: loaded from here, scoped `megavault:research-<role>`. npm/manual: copied to `~/.claude/agents/`. |
| `tests/run_offline.py` | Scenario harness, no runner needed. Tests register with `@test`. Tests find the skill as the one `skills/*/SKILL.md`, so a rename needs no test edit. |
| `tests/test_field_profile.py` | unittest suite for first-hand (field-note) validation. |
| `tests/test_run_stats.py` | unittest suite for `run_stats.py`, including the no-leak check. |
| `tests/test_installer.py` | unittest suite for `installer/cli.js`: install, `--force` and `uninstall` touch only the files the package ships; the Python version check never runs a program planted in the working directory; `--help` names the real install command (`npx github:bn0a/megavault`). Skipped without Node.js. |
| `tests/test_offline.py` | pytest shim over `run_offline.py`; adds no cases. |
| `tests/fixtures/mockbrain/` | Smallest vault root that exercises the conventions: router table, one prefixed vault, a decoy `qa/` note. |
| `USERMANUAL.md` | End-user manual: modes, gates, walkthroughs, troubleshooting. |
| `SECURITY.md` | How to report a vulnerability; supported version. |
| `installer/cli.js` | The npx installer (`npx github:bn0a/megavault install` / `uninstall`; the package is not on the npm registry), the secondary install path. Node built-ins only, no dependencies. Copies `skills/megavault/` and `agents/*.md` into a Claude Code config dir; `--force` and `uninstall` touch only the files the package ships. The skill directory name is the `SKILL_DIR` constant. Not in `bin/` on purpose: a plugin's top-level `bin/` is put on the Bash `PATH` while the plugin is enabled, and claude.ai/Cowork refuse plugins that have one. |
| `package.json` | npm package metadata. Its `files` whitelist decides what the tarball ships: `installer/`, `skills/`, `agents/`, `.claude-plugin/`, `README.md`, `LICENSE`. No dependencies and no lockfile (a lockfile would make Claude Code run a dependency install when it caches the plugin). |
| `.claude/CLAUDE.md` | Instructions for agents working on this repo. Kept out of the repository root because a root `CLAUDE.md` is a plugin-validation warning (it is not loaded for plugin users). |

npm/manual installs copy only `skills/megavault/` and `agents/*.md`. A plugin install copies the whole repository (minus `.git`) into Claude Code's plugin cache, but loads only `skills/` and `agents/`. Not in the npm tarball: `tests/`, `USERMANUAL.md`, `AGENTS.md`, `.claude/`.

## Invariants

Do not break these. A change that needs to is a design change; raise it, do not slip it in.

1. **Standard library only.** No third-party imports anywhere under `skills/megavault/scripts/` or `tests/`. Python 3.10 is the floor.
2. **One engine file.** `skills/megavault/scripts/research.py` stays a single file on purpose: it is the one place a reviewer checks for "did the model do something the script should have done." Do not split it into a package.
3. **The run store is the record.** `ingest` is the only writer of worker records in `records/` (`adopt` seeds the adopted ones); `verify` is the only thing that sets claim status. The one correction of a stored record is `relabel-edge` (an edge's relation, audited, followed by `verify`). Subagents only write inbox files and cache files.
4. **No claim without a cached verbatim quote.** The quote check (`exact` / `fuzzy` / `mismatch` / `no_cache` / `adopted`) is mechanical; never relax it to make a run pass.
5. **Never overwrite a published vault.** Writes into an existing vault use only the five additive verbs: `create`, `append-row`, `patch-cell`, `append-section`, `append-entry`.
6. **Tests pass before and after.** All four suites, offline.
7. **No personal data.** No machine-specific paths, user names or e-mails in code, tests, fixtures or docs. Paths come from env vars, CLI flags or `Path.home()` / `Path.cwd()`.
8. **Every artifact is English**: code comments, docs, generated vault text, test fixtures.
9. **Tokens are recorded, never estimated.** The engine measures no tokens; the only token numbers it prints are sums of `reports/token-ledger.jsonl` (what `budget --spend` or a result's `tokens` field recorded). Never add code or docs that estimate or invent a token number.

## Running the tests

From the repository root:

```sh
python tests/run_offline.py                        # scenario tests; exit code = failures
python tests/run_offline.py adopt                  # only tests whose name contains "adopt"
python -m unittest -v tests.test_field_profile
python -m unittest -v tests.test_run_stats
python -m unittest -v tests.test_installer          # needs Node.js; skipped without it
```

Tests build throwaway vaults under the system temp directory and remove them. No network, no model calls.

## Changing `skills/megavault/scripts/research.py` safely

1. Find the command's branch in `_main` (argparse setup, then one `if args.command == ...` block per command) and the functions it calls. Read the matching `skills/megavault/references/*.md` section; docs and code must agree.
2. Keep the invariants above. In particular, anything that changes claim status belongs in `verify`, and anything that writes records belongs in `ingest`.
3. Writes to run state go through the existing atomic JSON helpers; writes to vaults go through the merge-plan verbs. Do not add a raw `open(..., "w")` on a vault file.
4. Add or extend a test in `tests/run_offline.py` (decorate with `@test`; name it `test_<n>_<what_it_proves>`). A bug fix gets a test that fails without the fix.
5. If behaviour visible to the coordinator changes (a flag, an exit code, a JSON field), update `SKILL.md`, the relevant `references/*.md`, the agent definitions if their return shape changed, and `USERMANUAL.md`, in the same change.
6. Exit codes carry meaning: `0` ok, `1` gate or check failed, `2` hard stop (nothing written), `3` needs user confirmation. Keep them.

## Changing agent definitions

The return JSON shapes in `agents/*.md` are parsed by `ingest`. A field added there without engine support is silently useless; a field removed may break ingest. Change both together and run `tests/run_offline.py`.

## Release checklist

1. All four test suites pass; record the counts in `README.md` and `USERMANUAL.md` if they changed.
2. No build artifacts: no `__pycache__/`, `.pyc`, `vault/`, `.env`.
3. A grep over the repo returns no personal names, e-mails or local user paths.
4. `python skills/megavault/scripts/maintenance/run_stats.py --runs-root <a real runs root>` prints no run id, question, URL or path (the test covers this; spot-check anyway).
5. Bump `version` in `.claude-plugin/plugin.json` and `package.json` together, to the same value. Plugin users are pinned to the manifest `version`: without a bump, `claude plugin update` reports "already at the latest version". Do not set `version` in `marketplace.json`.
6. `claude plugin validate . --strict` (marketplace manifest) and `claude plugin validate .claude-plugin/plugin.json --strict` (plugin manifest and root) both pass.
7. Plugin install into a scratch config, never the real one: `CLAUDE_CONFIG_DIR=<tmp> claude plugin marketplace add <absolute repo path>`, then `CLAUDE_CONFIG_DIR=<tmp> claude plugin install megavault@megavault`, then `CLAUDE_CONFIG_DIR=<tmp> claude plugin details megavault` lists 1 skill and 4 agents. Delete `<tmp>` afterwards.
8. npm install into a scratch directory with `node installer/cli.js install --claude-dir <tmp>` and run `python <tmp>/skills/megavault/scripts/research.py --help`. Then `node installer/cli.js uninstall --claude-dir <tmp>` and check that nothing of ours is left (`tests/test_installer.py` covers this).
9. `npm pack --dry-run` and check the tarball contents: only `installer/cli.js`, `skills/megavault/` (`SKILL.md`, `scripts/` with `source_profiles.json` and `maintenance/`, `references/`), `agents/`, `.claude-plugin/`, `README.md`, `LICENSE`, `package.json`; no `tests/`, `__pycache__` or `.pyc`. The package is not published to the npm registry; users install with `npx github:bn0a/megavault`.
10. Numbers in README's "Measured on real runs" match a fresh `run_stats.py` output. Research data (run stores, topics, notes) is never committed; only aggregate counts appear, in the README.
