"""Independent Obsidian-style anchor checker. usage: anchor_audit.py <brain_root> <out.json>
Results are per vault: a top-level folder, or a vault inside a group folder (plants/<vault>)."""
import json, re, sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1])


# Kept independent of research.py on purpose: its own copy of the group rule.
def is_vault(d):
    return d.name == "field-notes" or (d / "90 Evidence").is_dir() or any(p.is_file() for p in d.glob("00 *Home.md"))


def grouped(folder):
    """Vaults inside a group folder (one that is no vault itself), through nested groups."""
    if is_vault(folder):
        return []
    out = []
    for c in sorted(x for x in folder.iterdir() if x.is_dir() and not x.name.startswith(".") and x.name != "research-staging"):
        out.extend([c] if is_vault(c) else grouped(c))
    return out


groups = {d.name: grouped(d) for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")}


def vault_of(p):
    rel = p.relative_to(root).parts
    if len(rel) <= 1:
        return "(root)"
    held = [v for v in groups.get(rel[0], ()) if v in p.parents]
    return held[0].relative_to(root).as_posix() if held else rel[0]

notes = {}
for p in root.rglob("*.md"):
    rel = p.relative_to(root).parts
    if any(x.startswith(".") for x in rel):
        continue
    notes.setdefault(p.stem.lower(), []).append(p)

LINK = re.compile(r"!?\[\[([^\[\]]+?)\]\]")
FENCE = re.compile(r"^\s*(```|~~~)")


def strip_code(text):
    out, fenced = [], False
    for line in text.replace("\r\n", "\n").split("\n"):
        if FENCE.match(line):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else re.sub(r"`[^`\n]*`", "", line))
    return out


def norm(s, lenient=False):
    s = s.strip()
    if lenient:
        s = re.sub(r"[#|^:\[\]%]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


cache = {}


def anchors_of(p):
    if p in cache:
        return cache[p]
    heads, blocks = [], set()
    for line in strip_code(p.read_text(encoding="utf-8", errors="replace")):
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            heads.append(m.group(2))
        b = re.search(r"(?:^|\s)\^([A-Za-z0-9-]+)\s*$", line)
        if b:
            blocks.add(b.group(1).lower())
    cache[p] = (heads, blocks)
    return cache[p]


res = defaultdict(lambda: {"cl_total": 0, "cl_dead": 0, "other_total": 0, "other_dead": 0, "dead_examples": []})
for stem, paths in notes.items():
    for p in paths:
        vault = vault_of(p)
        for line in strip_code(p.read_text(encoding="utf-8", errors="replace")):
            for m in LINK.finditer(line):
                inner = m.group(1).replace("\\|", "|").split("|")[0]  # `\|` = alias pipe in tables
                if "#" not in inner:
                    continue
                target, _, anchor = inner.partition("#")
                tstem = target.strip().split("/")[-1]
                if tstem.lower().endswith(".md"):
                    tstem = tstem[:-3]
                tp = p if not tstem else (notes.get(tstem.lower()) or [None])[0]
                if tp is None:
                    continue  # broken link, not an anchor problem
                last = anchor.split("#")[-1].strip()
                heads, blocks = anchors_of(tp)
                if last.startswith("^"):
                    ok = last[1:].lower() in blocks
                else:
                    ok = norm(last) in {norm(h) for h in heads} or norm(last, True) in {norm(h, True) for h in heads}
                is_cl = bool(re.fullmatch(r"CL-\d+", last))
                r = res[vault]
                key = "cl" if is_cl else "other"
                r[key + "_total"] += 1
                if not ok:
                    r[key + "_dead"] += 1
                    if len(r["dead_examples"]) < 5:
                        r["dead_examples"].append(f"{p.relative_to(root).as_posix()} -> [[{inner}]]")
json.dump(res, open(sys.argv[2], "w", encoding="utf-8"), indent=1, ensure_ascii=False)
tot = defaultdict(int)
for v, r in sorted(res.items()):
    for k in ("cl_total", "cl_dead", "other_total", "other_dead"):
        tot[k] += r[k]
    if r["cl_dead"] or r["other_dead"] or r["cl_total"]:
        print(f"{v:28} CL {r['cl_total']:5} dead {r['cl_dead']:5} | other {r['other_total']:4} dead {r['other_dead']:4}")
print("TOTAL", dict(tot))
