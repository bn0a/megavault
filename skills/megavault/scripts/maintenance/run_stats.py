#!/usr/bin/env python3
"""Aggregate counts over every run in a runs root. Read-only, stdlib only.

    python scripts/maintenance/run_stats.py [--runs-root DIR] [--json]

Default runs root: $RESEARCH_RUNS_ROOT, else ~/.claude/research-runs.

Privacy by construction: the report is built only from integers, dates and a
fixed allowlist of enum values (statuses, kinds, modes). Run ids, questions,
topics, URLs and paths are never put into it. As a second line of defence the
rendered text is checked against every run id, question, source URL and the
runs-root path before it is printed; any hit aborts with exit 3 and prints
nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

KINDS = ("research", "data", "mixed")
MODES = ("small", "standard", "vault", "expand")
CLAIM_STATUSES = ("supported", "qualified", "disputed", "unsupported", "refuted", "superseded")
QUOTE_CHECKS = ("exact", "fuzzy", "mismatch", "no_cache", "adopted")
HANDBACK_STATUSES = ("ok", "partial", "capped", "blocked")
ORIGINS = ("new", "adopted")


def default_runs_root() -> Path:
    override = os.environ.get("RESEARCH_RUNS_ROOT")
    return Path(override).expanduser() if override else Path.home() / ".claude" / "research-runs"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _records(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, dict)]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _bucket(value: Any, allowed: tuple[str, ...], missing: str = "unset") -> str:
    """Map a raw field to an allowlisted label. Free text never passes through."""
    if value is None or value == "":
        return missing
    return value if value in allowed else "other"


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _ordered(counter: Counter) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _dist(counter: Counter, order: tuple[str, ...]) -> dict[str, dict[str, float]]:
    total = sum(counter.values())
    keys = list(order) + sorted(k for k in counter if k not in order)
    return {k: {"n": counter[k], "pct": _pct(counter[k], total)} for k in keys if counter[k]}


def collect(runs_root: Path) -> tuple[dict, set[str]]:
    """Return (report, forbidden strings). The report holds aggregates only."""
    forbidden: set[str] = {str(runs_root), str(runs_root.resolve())}
    kinds: Counter = Counter()
    modes: Counter = Counter()
    claim_status: Counter = Counter()
    claim_origin: Counter = Counter()
    source_origin: Counter = Counter()
    quote_check: Counter = Counter()
    handbacks: Counter = Counter()
    domains_per_run: list[int] = []
    dates: list[str] = []
    totals = Counter()
    runs = 0

    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        manifest = _load(run_dir / "manifest.json")
        if not isinstance(manifest, dict):
            continue
        runs += 1
        forbidden.add(run_dir.name)
        for key in ("run_id", "question"):
            if isinstance(manifest.get(key), str) and manifest[key]:
                forbidden.add(manifest[key])
        kinds[_bucket(manifest.get("kind"), KINDS)] += 1
        modes[_bucket(manifest.get("mode"), MODES)] += 1
        created = manifest.get("created_at")
        if isinstance(created, str) and len(created) >= 10:
            dates.append(created[:10])

        taxonomy = _load(run_dir / "taxonomy.json")
        if isinstance(taxonomy, dict) and isinstance(taxonomy.get("domains"), list):
            domains_per_run.append(len(taxonomy["domains"]))

        records = run_dir / "records"
        sources = _records(_load(records / "sources.json"))
        claims = _records(_load(records / "claims.json"))
        excerpts = _records(_load(records / "excerpts.json"))
        notes = _records(_load(records / "notes.json"))
        totals.update(sources=len(sources), claims=len(claims), excerpts=len(excerpts), notes=len(notes))

        for source in sources:
            source_origin[_bucket(source.get("origin"), ORIGINS)] += 1
            for key in ("url", "canonical_url", "title"):
                if isinstance(source.get(key), str) and len(source[key]) > 8:
                    forbidden.add(source[key])
        for claim in claims:
            claim_status[_bucket(claim.get("status"), CLAIM_STATUSES)] += 1
            claim_origin[_bucket(claim.get("origin"), ORIGINS)] += 1
        for excerpt in excerpts:
            verification = excerpt.get("verification")
            status = verification.get("status") if isinstance(verification, dict) else None
            quote_check[_bucket(status, QUOTE_CHECKS, missing="unchecked")] += 1
        for handback in _records(_load(run_dir / "reports" / "handbacks.json")):
            handbacks[_bucket(handback.get("status"), HANDBACK_STATUSES)] += 1
            for item in handback.get("unread") or []:
                if isinstance(item, dict) and isinstance(item.get("url"), str) and len(item["url"]) > 8:
                    forbidden.add(item["url"])

    report = {
        "runs": runs,
        "first_run_date": min(dates) if dates else None,
        "last_run_date": max(dates) if dates else None,
        "kinds": _ordered(kinds),
        "modes": _ordered(modes),
        "domains_per_run": {
            "runs_with_taxonomy": len(domains_per_run),
            "min": min(domains_per_run) if domains_per_run else None,
            "median": statistics.median(domains_per_run) if domains_per_run else None,
            "max": max(domains_per_run) if domains_per_run else None,
        },
        "totals": {k: totals[k] for k in ("sources", "claims", "excerpts", "notes")},
        "source_origin": _ordered(source_origin),
        "claim_origin": _ordered(claim_origin),
        "claim_status": _dist(claim_status, CLAIM_STATUSES),
        "excerpt_quote_check": _dist(quote_check, QUOTE_CHECKS),
        "handback_status": _ordered(handbacks),
    }
    return report, {f for f in forbidden if f}


def render_text(report: dict) -> str:
    lines = [
        f"runs                {report['runs']}  ({report['first_run_date']} .. {report['last_run_date']})",
        f"kinds               {_inline(report['kinds'])}",
        f"modes               {_inline(report['modes'])}",
    ]
    d = report["domains_per_run"]
    lines.append(f"domains per run     min {d['min']}  median {d['median']}  max {d['max']}  (n={d['runs_with_taxonomy']})")
    t = report["totals"]
    lines.append(f"totals              sources {t['sources']}  claims {t['claims']}  excerpts {t['excerpts']}  notes {t['notes']}")
    lines.append(f"source origin       {_inline(report['source_origin'])}")
    lines.append(f"claim origin        {_inline(report['claim_origin'])}")
    lines.append("claim status")
    for key, value in report["claim_status"].items():
        lines.append(f"  {key:<17} {value['n']:>6}  {value['pct']:>5}%")
    lines.append("excerpt quote check")
    for key, value in report["excerpt_quote_check"].items():
        lines.append(f"  {key:<17} {value['n']:>6}  {value['pct']:>5}%")
    lines.append(f"handback status     {_inline(report['handback_status'])}")
    return "\n".join(lines) + "\n"


def _inline(counter: dict) -> str:
    return "  ".join(f"{k} {v}" for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))) or "-"


def leaks(text: str, forbidden: set[str]) -> int:
    """Number of forbidden strings (or URL schemes) present in the output."""
    hits = sum(1 for item in forbidden if item in text)
    return hits + (1 if "://" in text else 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate, privacy-safe counts over a runs root.")
    parser.add_argument("--runs-root", type=Path, default=default_runs_root())
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    runs_root: Path = args.runs_root.expanduser()
    if not runs_root.is_dir():
        print("runs root not found (pass --runs-root or set RESEARCH_RUNS_ROOT)", file=sys.stderr)
        return 1
    report, forbidden = collect(runs_root)
    text = json.dumps(report, indent=2) + "\n" if args.json else render_text(report)
    hits = leaks(text, forbidden)
    if hits:
        print(f"refusing to print: {hits} identifying string(s) would leak into the report", file=sys.stderr)
        return 3
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
