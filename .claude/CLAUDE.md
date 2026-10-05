# CLAUDE.md

You are working on the source of the `megavault` skill, not running it.

- Rules for changing this repo (invariants, tests, release checklist): `AGENTS.md`. Follow it.
- What the skill does at runtime: `skills/megavault/SKILL.md` and `agents/*.md`. Those files are loaded by Claude Code when the skill is installed, so edits to them change live behaviour. Keep them short; detail goes in `skills/megavault/references/`.
- Do not invoke `/megavault` or `/megavault:megavault` (the installed skill) to test a change here. Use the offline tests; a real run costs millions of tokens and writes to `~/.claude/research-runs/`.
- Do not install into the real `~/.claude` while testing. Use `node installer/cli.js install --claude-dir <scratch>` (add `--dry-run` to only print the plan), or for the plugin path run `claude plugin ...` with `CLAUDE_CONFIG_DIR=<scratch>`. Never run `npm publish`; releases are the maintainer's step.
- Never read or print the contents of a runs root beyond what `skills/megavault/scripts/maintenance/run_stats.py` outputs.
