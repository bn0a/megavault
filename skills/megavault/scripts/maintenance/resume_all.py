#!/usr/bin/env python3
"""Every open /megavault run and how to continue it.

Read-only: reads <runs root>/ACTIVE.json (kept by research.py), executes nothing,
writes nothing. Runs created before ACTIVE.json existed are listed by id; one
`/megavault resume <id>` registers such a run.

    python scripts/maintenance/resume_all.py [--runs-root DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def default_runs_root() -> Path:
    override = os.environ.get("RESEARCH_RUNS_ROOT")
    return Path(override).expanduser() if override else Path.home() / ".claude" / "research-runs"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-root", type=Path, default=default_runs_root())
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    path = args.runs_root / "ACTIVE.json"
    registry: dict = {}
    if path.is_file():
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path} is unreadable: {exc}", file=sys.stderr)
            return 1
    runs = registry.get("open") or []
    known = {e.get("run_id") for e in runs} | {e.get("run_id") for e in registry.get("closed") or []}
    legacy = sorted(
        (m.parent for m in args.runs_root.glob("*/manifest.json") if m.parent.name not in known),
        key=lambda d: (d / "manifest.json").stat().st_mtime, reverse=True,
    )

    if args.json:
        print(json.dumps({"open": runs, "unregistered": [d.name for d in legacy]}, indent=2, ensure_ascii=False))
        return 0
    print(f"{len(runs)} open run(s) in {path}")
    for entry in runs:
        rid = entry.get("run_id")
        missing = "" if Path(str(entry.get("run_dir") or "")).is_dir() else "  [run directory missing]"
        failed = f"  [exit {entry['last_exit']}]" if entry.get("last_exit") not in (0, None) else ""
        print(f"\n{rid}{missing}")
        print(f"  question : {entry.get('question')}")
        print(f"  status   : {entry.get('status')}  (updated {entry.get('updated')})")
        print(f"  last     : {entry.get('last_command')}{failed}")
        print(f"  next     : {entry.get('next_command')}")
        print(f"  continue : /megavault resume {rid}")
    if legacy:
        shown = ", ".join(d.name for d in legacy[:5]) + (" ..." if len(legacy) > 5 else "")
        print(f"\n{len(legacy)} run(s) not in ACTIVE.json (newest first; `/megavault resume <id>` registers one): {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
