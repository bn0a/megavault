# Security policy

## Supported version

Only the latest commit on the `main` branch is supported. Fixes land there; earlier versions get no
backports. Update to the latest version before you report, and check that the problem is still there.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting on this repository (the
**Security** tab, then **Report a vulnerability**).

If that option is not available, open a GitHub issue that says a security problem exists and names
the affected area, with no exploit details, proof of concept or payload. A private channel is then
arranged from that issue.

## What to include

- The version (`version` in `.claude-plugin/plugin.json`) or commit, and the install path: plugin,
  npx or manual copy.
- Your operating system and your Python and Claude Code versions.
- What an attacker controls (for example a web page a run fetches, a vault file, a run directory)
  and what they gain.
- Steps to reproduce, ideally offline: a small page or fixture under an `.example` domain, or a
  failing case for `tests/run_offline.py`.

## Scope

This tool fetches untrusted web pages and passes their text to subagents that hold `Bash`, so
reports about prompt-injection paths are in scope: page text that leads a subagent to run a
command, write outside the run directory, or place content in a vault that forges a row or a
status. So is any way around what the engine enforces whatever the model does (listed under
*Limits and security* in [USERMANUAL.md](USERMANUAL.md#limits-and-security)), and any case of the
installer touching files it does not ship.

Out of scope: problems that need the documented safeguards switched off (for example
`--dangerously-skip-permissions` or blanket `Bash(*)` allow rules), the factual accuracy of a
claim (a status says how well a claim is backed by checked quotes, not whether it is true), and
vulnerabilities in Claude Code itself, which belong with Claude Code's own reporting channel.

## Response

This project has a single maintainer. Reports are read and answered on a best-effort basis: there
is no service-level agreement and no guaranteed response or fix time. A confirmed problem is fixed
on `main` or, if it is accepted as a known limit, documented under *Limits and security* in
[USERMANUAL.md](USERMANUAL.md#limits-and-security). The report is updated either way.
