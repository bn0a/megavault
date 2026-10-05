"""Retrofit a `## Claim index` (one `### CL-###` heading per claim) into every
parseable Claim Ledger under a brain root, so `[[<Prefix> Claim Ledger#CL-###]]`
links land on the claim in Obsidian.

Idempotent: a ledger that already has an index only gets the entries it lacks;
a second run changes nothing. Dry run by default; --apply writes. Preserves each
file's newline style. Uses the skill's own renderer so retrofitted
ledgers are byte-identical in shape to what `publish`/`merge-plan` now emit.

Usage: python retrofit_claim_index.py <brain_root> [--apply] [--skill <research.py>]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SKILL = HERE.parent / "research.py"


def load(skill: Path):
    spec = importlib.util.spec_from_file_location("research_mod", skill)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def retrofit(R, ledger: Path, apply: bool) -> dict:
    text, newline = R.read_note(ledger)
    shape = R.read_id_table(ledger, "CL")
    if not shape["found"] or not shape["rows"]:
        return {"ledger": str(ledger), "rows": 0, "added": 0, "skipped": "no CL-### table"}
    header = shape["header"]
    text_col = R._column(header, "claim", "text")
    items = []
    for cid in shape["order"]:
        cells = shape["rows"][cid]["cells"]
        items.append((cid, R._cell(cells, text_col if text_col is not None else 1)))
    indexed = set(R.claim_index_ids(text))
    missing = [(cid, t) for cid, t in items if cid not in indexed]
    result = {"ledger": str(ledger), "rows": len(items), "already_indexed": len(indexed), "added": len(missing)}
    if not missing:
        return result
    map_name = ledger.stem.replace(" Claim Ledger", " Evidence Map")
    if not (ledger.parent / f"{map_name}.md").is_file():
        map_name = None
    lines = text.split("\n")
    has_section = any(line.strip() == R.CLAIM_INDEX_HEADING for line in lines)
    if has_section:
        anchor = R.claim_index_tail(text)
        block = "".join(R.claim_index_entry(cid, t, map_name) for cid, t in missing)
    else:
        anchor = shape["last_raw"]
        block = R.render_claim_index(map_name, missing)
    hits = [i for i, line in enumerate(lines) if line == anchor] if anchor else []
    if len(hits) != 1:
        result["error"] = f"anchor not unique ({len(hits)} hits): {anchor!r}"
        result["added"] = 0
        return result
    new_lines = block.split("\n")
    if new_lines and new_lines[-1] == "":
        new_lines = new_lines[:-1]
    # Byte-preserving insert: every existing line keeps its own ending (files
    # may mix LF and CRLF); inserted lines take the anchor line's ending.
    raw = ledger.read_bytes().decode("utf-8")
    raw_lines = re.findall(r"[^\n]*\n|[^\n]+$", raw)
    assert len(raw_lines) >= hits[0] + 1 and raw_lines[hits[0]].rstrip("\r\n") == anchor
    eol = raw_lines[hits[0]][len(anchor):] or newline
    if not raw_lines[hits[0]].endswith(("\n", "\r")):
        raw_lines[hits[0]] += newline  # anchor was the last line without an ending
        eol = newline
    raw_lines[hits[0] + 1 : hits[0] + 1] = [line + eol for line in new_lines]
    if apply:
        ledger.write_bytes("".join(raw_lines).encode("utf-8"))
    result["mode"] = "applied" if apply else "dry-run"
    result["section"] = "extended" if has_section else "created"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retrofit_claim_index.py",
        description="Add a `## Claim index` under every Claim Ledger table in a brain. "
                    "Dry run unless --apply.",
    )
    parser.add_argument("brain", type=Path, help="the brain (vault) root")
    parser.add_argument("--apply", action="store_true", help="write the ledgers (default: dry run)")
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL, help="path to research.py")
    args = parser.parse_args(argv)
    apply = args.apply
    brain = args.brain.expanduser().resolve()
    if not brain.is_dir():
        parser.error(f"not a directory: {brain}")
    R = load(args.skill)
    out = []
    # top-level vaults as always, plus vaults inside group folders (plants/<vault>/)
    ledgers = set(brain.glob("*/90 Evidence/* Claim Ledger.md"))
    for vault in R.brain_vault_dirs(brain):
        ledgers.update(vault.glob("90 Evidence/* Claim Ledger.md"))
    for ledger in sorted(ledgers):
        if any(part.startswith(".") for part in ledger.relative_to(brain).parts):
            continue
        out.append(retrofit(R, ledger, apply))
    for item in out:
        rel = Path(item["ledger"]).relative_to(brain).as_posix()
        print(f"{rel:70} rows={item['rows']:4} added={item['added']:4} "
              f"{item.get('section', item.get('skipped', ''))} {item.get('error', '')}")
    print(json.dumps({"ledgers": len(out), "added": sum(i["added"] for i in out),
                      "errors": [i for i in out if i.get("error")]}, indent=1))
    return 1 if any(i.get("error") for i in out) else 0


if __name__ == "__main__":
    sys.exit(main())
