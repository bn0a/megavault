"""Read-only anchor checker: every [[Target#Anchor]] wikilink in a brain must land on
a heading (or ^block id) in the target note. Obsidian semantics: '#Heading' matches a
heading's full text; '#A#B' names nested headings (last segment must exist);
'#^id' is a block id. Headings inside fenced code are not headings.
Usage: python check_anchors.py <brain_root> [--json out.json] [--list N]
Counts are per vault: a top-level folder, or a vault inside a group folder (plants/<vault>)."""
import importlib.util, json, re, sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("r", Path(__file__).resolve().parent.parent / "research.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

# `\|` (escaped alias pipe in table rows) separates like `|`
LINK = re.compile(r"!?\[\[([^\]|#\\]*)#((?:[^\]|\\]|\\(?!\|))+)(?:\\?\|[^\]]*)?\]\]")
HEAD = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
BLOCK = re.compile(r"\s\^([A-Za-z0-9-]+)\s*$")

def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()

def targets(path):
    heads, blocks, fenced = set(), set(), False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced; continue
        if fenced: continue
        m = HEAD.match(line)
        if m: heads.add(norm(m.group(1)))
        b = BLOCK.search(line)
        if b: blocks.add(b.group(1).lower())
        if re.fullmatch(r"\^[A-Za-z0-9-]+", line.strip()): blocks.add(line.strip()[1:].lower())
    return heads, blocks

def main():
    brain = Path(sys.argv[1]); out = None; nlist = 0
    if "--json" in sys.argv: out = Path(sys.argv[sys.argv.index("--json") + 1])
    if "--list" in sys.argv: nlist = int(sys.argv[sys.argv.index("--list") + 1])
    files = [p for p in brain.rglob("*.md") if not any(x.startswith(".") for x in p.relative_to(brain).parts)]
    groups = {g.name: R.group_vaults(g) for g in R.brain_group_dirs(brain)}

    def vault_of(p, rel):
        if len(rel.parts) <= 1:
            return "(root)"
        held = [v for v in groups.get(rel.parts[0], ()) if v in p.parents]
        return held[0].relative_to(brain).as_posix() if held else rel.parts[0]
    by = {}
    for p in files: by.setdefault(p.stem, []).append(p)
    cache = {}
    per = {}; bad_examples = []
    for p in files:
        rel = p.relative_to(brain); top = vault_of(p, rel)
        fenced = False
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("```"):
                fenced = not fenced; continue
            if fenced: continue
            for m in LINK.finditer(line):
                name = m.group(1).strip().split("/")[-1] or p.stem
                anchor = m.group(2).strip()
                stat = per.setdefault(top, {"anchors": 0, "unresolved": 0, "cl_anchors": 0, "cl_unresolved": 0})
                is_cl = bool(re.fullmatch(r"CL-\d+", anchor.split("#")[-1].strip()))
                stat["anchors"] += 1; stat["cl_anchors"] += is_cl
                tp = by.get(name)
                ok = False
                if tp:
                    if tp[0] not in cache: cache[tp[0]] = targets(tp[0])
                    heads, blocks = cache[tp[0]]
                    last = anchor.split("#")[-1].strip()
                    ok = (last[1:].lower() in blocks) if last.startswith("^") else (norm(last) in heads)
                if not ok:
                    stat["unresolved"] += 1; stat["cl_unresolved"] += is_cl
                    if len(bad_examples) < 2000: bad_examples.append(f"{rel.as_posix()}: [[{name}#{anchor}]]")
    tot = {k: sum(v[k] for v in per.values()) for k in ("anchors", "unresolved", "cl_anchors", "cl_unresolved")}
    for k in sorted(per): print(f"{k:24} anchors={per[k]['anchors']:5} unresolved={per[k]['unresolved']:5} (CL {per[k]['cl_unresolved']}/{per[k]['cl_anchors']})")
    print("TOTAL", tot)
    for e in bad_examples[:nlist]: print("  ", e)
    if out: out.write_text(json.dumps({"per_vault": per, "total": tot, "unresolved": bad_examples}, indent=1), encoding="utf-8")
    return 0 if tot["unresolved"] == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
