"""Read-only per-vault counts (no fixes):
(a) claims without any source, (b) Source Register entries without a URL,
(c) non-English text in agent-readable notes: filenames, headings, prose.
Usage: python content_stats.py <brain_root> [--json out.json]
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "research.py"
spec = importlib.util.spec_from_file_location("r", SKILL)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def non_english(line: str) -> bool:
    """The engine's rule (research.py is_non_english): ≥ 2 lower-case words with a
    non-ASCII letter, ≥ 2 non-English stop words, or one of each."""
    return R.is_non_english(line)


def non_english_name(stem: str) -> bool:
    return any(ch.isalpha() and not ch.isascii() for ch in stem) or non_english(stem)


brain = Path(sys.argv[1]).resolve()
rows = []
# every top-level folder, except that a group folder (plants/, …) gives way to its vaults
tops = sorted(p for p in brain.iterdir() if p.is_dir() and not p.name.startswith("."))
for vault in [v for top in tops for v in (R.group_vaults(top) or [top])]:
    names = R._meta_names(vault) if (vault / "90 Evidence").is_dir() else {}
    ev = vault / "90 Evidence"
    ledger = R.parse_claim_ledger(ev / f"{names.get('claim_ledger', '')}.md") if names else {}
    register = R.parse_source_register(ev / f"{names.get('source_register', '')}.md") if names else {}
    no_source = sorted(cid for cid, c in ledger.items() if not c["source_ids"])
    no_url = sorted(sid for sid, s in register.items() if not s.get("url"))
    files = [p for p in vault.rglob("*.md") if not any(x.startswith(".") for x in p.relative_to(vault).parts)]
    ne_names, ne_heads, ne_prose, ne_quote, ne_files = [], 0, 0, 0, set()
    for f in files:
        if non_english_name(f.stem):
            ne_names.append(f.relative_to(vault).as_posix())
        fenced = False
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced or not non_english(line):
                continue
            ne_files.add(f)
            s = line.strip()
            if s.startswith(">") or f.stem.startswith("Quote - ") or re.search(r"[\"“].{12,}[\"”]", s):
                ne_quote += 1
            elif s.startswith("#"):
                ne_heads += 1
            else:
                ne_prose += 1
    rows.append({"vault": vault.relative_to(brain).as_posix(), "claims": len(ledger), "claims_without_source": len(no_source),
                 "sources": len(register), "sources_without_url": len(no_url),
                 "non_en_filenames": len(ne_names), "non_en_heading_lines": ne_heads, "non_en_prose_lines": ne_prose,
                 "non_en_quote_lines": ne_quote, "non_en_files": len(ne_files), "non_en_filename_list": ne_names[:10],
                 "no_url_ids": no_url[:15]})
root_ne = []
for f in sorted(brain.glob("*.md")):
    n = sum(1 for l in f.read_text(encoding="utf-8", errors="replace").splitlines() if non_english(l))
    if n:
        root_ne.append((f.name, n))
print(f"{'vault':22} {'claims':>6} {'noSrc':>6} {'srcs':>5} {'noURL':>6} | "
      f"{'nonEnName':>9} {'nonEnHead':>9} {'nonEnProse':>10} {'nonEnQuote':>10} {'nonEnFiles':>10}")
for r in rows:
    print(f"{r['vault']:22} {r['claims']:6} {r['claims_without_source']:6} {r['sources']:5} {r['sources_without_url']:6} | "
          f"{r['non_en_filenames']:9} {r['non_en_heading_lines']:9} {r['non_en_prose_lines']:10} "
          f"{r['non_en_quote_lines']:10} {r['non_en_files']:10}")
print("root files with non-English lines:", root_ne)
if "--json" in sys.argv:
    Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps({"vaults": rows, "root": root_ne}, indent=1, ensure_ascii=False), encoding="utf-8")
