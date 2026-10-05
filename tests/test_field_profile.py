"""Unit tests for the first-hand validation (field-note, case-study, practice-guide,
first-hand areas, mixed vaults, callouts) in research.py.

Each test builds a throwaway brain with one topic vault and a field-notes area
holding one GOOD note, then mutates exactly one thing and asserts the specific
error (or warning). Run:  python -m unittest -v test_field_profile
Temp dirs go under $TMP.
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

# The skill lives in skills/<name>/; found by its SKILL.md so a rename needs no edit here.
SKILL = next((Path(__file__).resolve().parent.parent / "skills").glob("*/SKILL.md")).parent / "scripts" / "research.py"
_spec = importlib.util.spec_from_file_location("research_field", SKILL)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

GOOD_FM = {
    "type": "field-note",
    "kind": "gotcha",
    "status": "active",
    "scope": "general",
    "evidence_number": "measured",
    "evidence_generalization": "observed",
    "conditions": "Python 3.10+, any OS",
    "sample": "3 runs on 2 machines",
    "artifacts": ["scripts/moisture_log.py@1a2b3c4d", "logs/run-01.json"],
    "measured_at": "2026-01-15",
    "invalidated_by": ["decision:DEC-7", "version:planter-firmware@2.4"],
    "supersedes": [],
    "superseded_by": "",
    "origin": "test fixture",
    "published": "2026-01-15",
    "last_verified": "2026-01-15",
    "tags": ["test"],
}
GOOD_BODY = """
# Good Note

## Claim
A tool that rewrites line endings turns an insert into a whole-file diff.

## When it does not hold
Files with one consistent line ending.

## Evidence
See the JSON log.

## How to reuse
Insert with endings kept.

## Related
- [[Pothos Note]]
- [[Field Notes - Tooling MOC]]
"""


def render_fm(fm: dict) -> str:
    lines = ["---"]
    for key, value in fm.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                lines += [f"  - {item}" for item in value]
            else:
                lines.append(f"{key}: []")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FieldProfile(unittest.TestCase):
    def setUp(self) -> None:
        self.brain = Path(tempfile.mkdtemp(prefix="field-profile-"))
        b = self.brain
        write(b / "README.md", "# Brain\n\n## Router\n\n| Vault | When | Entry |\n|---|---|---|\n")
        write(b / "pothos" / "00 Pothos Home.md", "---\ntype: home\n---\n# Pothos\n\n[[01 Pothos MOC]]\n")
        write(b / "pothos" / "01 Pothos" / "01 Pothos MOC.md", "---\ntype: moc\n---\n# MOC\n\n[[Pothos Note]]\n")
        write(b / "pothos" / "01 Pothos" / "Pothos Note.md", "---\ntype: concept\n---\n# Pothos Note\n\n## Heading A\n")
        self.field = b / "field-notes"
        write(self.field / "00 Field Notes Home.md",
              "---\ntype: home\n---\n# Home\n\n- [[Field Notes - Tooling MOC]]\n- [[Field Notes Schema]]\n")
        write(self.field / "99 Meta" / "Field Notes Schema.md",
              "---\ntype: meta\n---\n# Schema\n\n```markdown\n[[Not A Real Note]]\n```\n")
        write(self.field / "01 Tooling" / "Field Notes - Tooling MOC.md",
              "---\ntype: moc\n---\n# Tooling\n\n- [[Good Note]]\n")
        self.note = self.field / "01 Tooling" / "Good Note.md"
        self.set_note()

    def tearDown(self) -> None:
        shutil.rmtree(self.brain, ignore_errors=True)

    # helpers ---------------------------------------------------------------
    def set_note(self, fm_changes: dict | None = None, drop: tuple = (), body: str = GOOD_BODY) -> None:
        fm = dict(GOOD_FM)
        fm.update(fm_changes or {})
        for key in drop:
            fm.pop(key)
        write(self.note, render_fm(fm) + body)

    def run_profile(self) -> dict:
        return R.validate_field_notes(self.field, self.brain)

    def assert_error(self, fragment: str) -> None:
        report = self.run_profile()
        hits = [e for e in report["errors"] if fragment in e]
        self.assertTrue(hits, f"expected an error containing {fragment!r}; got {report['errors']}")
        self.assertFalse(report["valid"])

    def assert_warning(self, fragment: str) -> None:
        report = self.run_profile()
        self.assertEqual(report["errors"], [], "a warning case must not also produce errors")
        self.assertTrue([w for w in report["warnings"] if fragment in w],
                        f"expected a warning containing {fragment!r}; got {report['warnings']}")

    # positive ----------------------------------------------------------------
    def test_00_good_note_passes_clean(self):
        report = self.run_profile()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["field_notes"], 1)

    def test_01_superseded_note_with_existing_successor_passes(self):
        write(self.field / "01 Tooling" / "Newer Note.md",
              render_fm(GOOD_FM) + GOOD_BODY.replace("# Good Note", "# Newer Note"))
        moc = self.field / "01 Tooling" / "Field Notes - Tooling MOC.md"
        write(moc, moc.read_text(encoding="utf-8") + "- [[Newer Note]]\n")
        self.set_note({"status": "superseded", "superseded_by": "[[Newer Note]]"})
        self.assertEqual(self.run_profile()["errors"], [])

    def test_02_cli_profile_and_brain_include(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(["--runs-root", str(self.brain / ".runs"), "validate", "--profile", "field",
                           "--brain", str(self.brain)])
        self.assertEqual(code, 0, out.getvalue())
        brain_report = R.validate_brain(self.brain)
        self.assertIn("field-notes", [v["vault"] for v in brain_report["vaults"]])
        self.set_note({"kind": "bogus"})
        self.assertFalse(R.validate_brain(self.brain)["valid"])

    def test_03_topic_vault_inside_a_group_folder(self):
        """In a group folder the topic vault is the grouped vault, never the group."""
        b = self.brain
        (b / "plants").mkdir()
        shutil.move(str(b / "pothos"), str(b / "plants" / "pothos"))
        report = self.run_profile()
        self.assertEqual((report["errors"], report["warnings"]), ([], []), "plants/pothos is a topic vault")
        # a note loose in the group itself sits in no topic vault
        write(b / "plants" / "Loose Note.md", "---\ntype: concept\n---\n# Loose Note\n")
        self.set_note(body=GOOD_BODY.replace("[[Pothos Note]]", "[[Loose Note]]"))
        self.assert_warning("links into no topic vault")
        # nor does a first-hand area inside a group
        write(b / "notes" / "lab" / "00 Lab Home.md",
              "---\ntype: home\nvault_kind: first-hand\n---\n# Lab\n\n[[Lab Note]]\n")
        write(b / "notes" / "lab" / "Lab Note.md", "---\ntype: concept\n---\n# Lab Note\n")
        self.set_note(body=GOOD_BODY.replace("[[Pothos Note]]", "[[Lab Note]]"))
        self.assert_warning("links into no topic vault")

    # negative (errors) ---------------------------------------------------------
    def test_10_missing_required_key(self):
        self.set_note(drop=("origin",))
        self.assert_error("frontmatter missing 'origin'")

    def test_11_bad_kind_enum(self):
        self.set_note({"kind": "anecdote"})
        self.assert_error("kind must be one of")

    def test_12_bad_status_enum(self):
        self.set_note({"status": "stale"})
        self.assert_error("status must be one of")

    def test_13_bad_scope_enum(self):
        self.set_note({"scope": "universal"})
        self.assert_error("scope must be one of")

    def test_14_bad_evidence_enum(self):
        self.set_note({"evidence_generalization": "proven"})
        self.assert_error("evidence_generalization must be one of")

    def test_15_empty_artifacts(self):
        self.set_note({"artifacts": []})
        self.assert_error("artifacts must not be empty")

    def test_16_empty_invalidated_by(self):
        self.set_note({"invalidated_by": ""})
        self.assert_error("invalidated_by must not be empty")

    def test_17_empty_conditions(self):
        self.set_note({"conditions": ""})
        self.assert_error("conditions must not be empty")

    def test_18_superseded_without_successor(self):
        self.set_note({"status": "superseded"})
        self.assert_error("status superseded needs superseded_by")

    def test_19_superseded_by_missing_note(self):
        self.set_note({"status": "superseded", "superseded_by": "Ghost Note"})
        self.assert_error("superseded_by names 'Ghost Note', which does not exist")

    def test_20_benchmark_without_measured_at(self):
        self.set_note({"kind": "benchmark", "measured_at": ""})
        self.assert_error("kind benchmark needs measured_at")

    def test_21_missing_body_heading(self):
        self.set_note(body=GOOD_BODY.replace("## When it does not hold", "## Limits"))
        self.assert_error("missing body heading '## When it does not hold'")

    def test_22_related_needs_two_resolving_links(self):
        self.set_note(body=GOOD_BODY.replace("- [[Field Notes - Tooling MOC]]\n", ""))
        self.assert_error("'## Related' needs at least 2 wikilinks that resolve")

    def test_23_dangling_wikilink(self):
        self.set_note(body=GOOD_BODY + "\nSee [[Nobody Wrote This]].\n")
        self.assert_error("dangling wikilink [[Nobody Wrote This]]")

    def test_24_dangling_anchor(self):
        self.set_note(body=GOOD_BODY + "\nSee [[Pothos Note#Heading B]].\n")
        self.assert_error("anchor [[Pothos Note#Heading B]] lands on no heading")

    def test_25_not_linked_from_home_or_moc(self):
        write(self.field / "01 Tooling" / "Field Notes - Tooling MOC.md", "---\ntype: moc\n---\n# Tooling\n")
        self.assert_error("not linked from [[00 Field Notes Home]] or any field-notes MOC")

    def test_26_basename_not_unique_brain_wide(self):
        write(self.brain / "pothos" / "01 Pothos" / "Good Note.md", "---\ntype: concept\n---\n# dup\n")
        self.assert_error("is not unique brain-wide")

    def test_27_non_ascii_filename(self):
        bad = self.field / "01 Tooling" / "Übersicht Notiz.md"
        write(bad, render_fm(GOOD_FM) + GOOD_BODY)
        moc = self.field / "01 Tooling" / "Field Notes - Tooling MOC.md"
        write(moc, moc.read_text(encoding="utf-8") + "- [[Übersicht Notiz]]\n")
        self.assert_error("filename must be plain ASCII English")

    def test_28_wrong_type(self):
        self.set_note({"type": "concept"})
        self.assert_error("must be 'type: field-note'")

    def test_29_malformed_machine_trigger(self):
        self.set_note({"invalidated_by": ["version:planter"]})
        self.assert_error("starts like a machine-checkable trigger but is malformed")

    def test_30_moc_without_prefix(self):
        (self.field / "01 Tooling" / "Field Notes - Tooling MOC.md").rename(self.field / "01 Tooling" / "Tooling MOC.md")
        self.assert_error("MOC basename must start with 'Field Notes - '")

    def test_31_moc_not_linked_from_home(self):
        write(self.field / "00 Field Notes Home.md", "---\ntype: home\n---\n# Home\n\n[[Field Notes Schema]]\n")
        self.assert_error("MOC is not linked from [[00 Field Notes Home]]")

    def test_32_missing_frontmatter(self):
        write(self.note, GOOD_BODY)
        self.assert_error("missing or unparseable frontmatter")

    # warnings -----------------------------------------------------------------
    def test_40_long_body_warns(self):
        self.set_note(body=GOOD_BODY + "\n" + ("word " * 650) + "\n")
        self.assert_warning("words (> 600)")

    def test_41_no_topic_vault_link_warns(self):
        moc = self.field / "01 Tooling" / "Field Notes - Tooling MOC.md"
        # a valid note whose Related links stay inside field-notes
        write(self.field / "01 Tooling" / "Other Note.md",
              render_fm(GOOD_FM) + GOOD_BODY.replace("# Good Note", "# Other Note").replace("[[Pothos Note]]", "[[Good Note]]"))
        write(moc, moc.read_text(encoding="utf-8") + "- [[Other Note]]\n")
        report = self.run_profile()
        self.assertEqual(report["errors"], [])
        self.assertTrue([w for w in report["warnings"] if "Other Note.md" in w and "no topic vault" in w])

    def test_42_measured_generalization_on_single_run_warns(self):
        self.set_note({"evidence_generalization": "measured", "sample": "n=1 chain"})
        self.assert_warning("single chain/run")

    def test_43_unrecognised_artifact_warns(self):
        self.set_note({"artifacts": ["a note from yesterday"]})
        self.assert_warning("artifact 'a note from yesterday'")

    def test_44_review_due_not_required(self):
        # the vault gate needs review_due; the field gate must not
        self.set_note()
        self.assertNotIn("review_due", " ".join(self.run_profile()["errors"]))

    def test_45_code_block_links_are_ignored(self):
        # the schema file holds [[Not A Real Note]] inside a fence
        self.assertFalse([e for e in self.run_profile()["errors"] if "Not A Real Note" in e])


# ==========================================================================
# first-hand pages (case-study / practice-guide), areas, mixed vaults, callouts
# ==========================================================================

TABLE = """| | |
|---|---|
| Kind | {kind} |
| Scope | {scope} |
| Evidence (number / generalization) | {grades} |
| Sample | {sample} |
| Invalidated by | {invalidated} |
| Supersedes | {supersedes} |
| Bears on | {bears} |
"""
ROW_DEFAULTS = dict(kind="finding", scope="domain: fixture cuttings", grades="measured / observed",
                    sample="3 runs of 500 cuttings", invalidated="a change to care-plan.md",
                    supersedes="none", bears="[[Pothos Note]] — refines — fixture")


def table(**overrides) -> str:
    values = dict(ROW_DEFAULTS)
    values.update(overrides)
    return TABLE.format(**values)


PAGE_FM = {
    "type": "case-study", "status": "active", "project": "Fixture", "period": '"2026-01-13..2026-01-15"',
    "topics": "[Growing Variables, mini]", "bears_on": '["Pothos Note"]',
    "artifacts": ["logs/run-01.json @1a2b3c4d"], "origin": '"Fixture, run 1"',
    "published": "2026-01-15", "last_verified": "2026-01-15", "tags": "[field-note, case-study]",
}


def page_fm(**changes) -> str:
    fm = dict(PAGE_FM)
    for key, value in changes.items():
        if value is None:
            fm.pop(key, None)
        else:
            fm[key] = value
    lines = ["---"]
    for key, value in fm.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines += [f"  - {item}" for item in value]
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines + ["---", ""])


def case_body(title="Case Study - Alpha", lesson="Lesson - Measure Twice", rows=None, extra="",
              drop_heading=None) -> str:
    rows = table() if rows is None else rows
    parts = {
        "## What we set out to learn": "We wanted to know.",
        "## Setup and conditions": "One machine, 3 workers.",
        "## What we measured": "| a | b |\n|---|---|\n| 1 | 2 |",
        "## Lessons": f"### {lesson}\n\nClaim text. See [[Pothos Note]].\n\n{rows}{extra}",
        "## What stayed open": "- More seeds.",
        "## Related": "- [[Pothos Note]]",
    }
    body = [f"# {title}", ""]
    for heading, content in parts.items():
        if heading == drop_heading:
            continue
        body += [heading, "", content, ""]
    return "\n".join(body)


def guide_body(title="Guide - Beta", drop_heading=None) -> str:
    parts = {
        "## When to use this": "When a watering log spans many weeks.",
        "## Procedure": "### Lesson - Check The Drainage\n\n1. Check the drainage holes.\n\n" + table(kind="procedure", scope="general"),
        "## Traps": "### Trap - Sealed Saucer\n\nWhat happened.\n\n" + table(kind="gotcha", grades="observed / reasoned", bears="none — tooling; see [[Case Study - Alpha]]"),
        "## Related": "- [[Case Study - Alpha]]",
    }
    body = [f"# {title}", ""]
    for heading, content in parts.items():
        if heading == drop_heading:
            continue
        body += [heading, "", content, ""]
    return "\n".join(body)


CALLOUT = ("> [!example] Tested first-hand\n"
           "> We measured it on 3 runs.\n"
           "> Details and data: [[{target}#{anchor}]]\n")


class FirstHandPages(unittest.TestCase):
    """A brain with a research vault (demo), the field-notes area holding one case
    study, and a declared first-hand vault (bench) holding one practice guide."""

    def setUp(self) -> None:
        self.brain = Path(tempfile.mkdtemp(prefix="first-hand-"))
        b = self.brain
        write(b / "README.md", "# Brain\n\n## Router\n\n| Vault | When | Entry |\n|---|---|---|\n")
        write(b / "pothos" / "00 Pothos Home.md", "---\ntype: home\n---\n# Pothos\n\n[[01 Pothos MOC]]\n")
        write(b / "pothos" / "01 Pothos" / "01 Pothos MOC.md", "---\ntype: moc\n---\n# MOC\n\n[[Pothos Note]]\n")
        self.pothos_note = b / "pothos" / "01 Pothos" / "Pothos Note.md"
        write(self.pothos_note, "---\ntype: concept\n---\n# Pothos Note\n\nThe anchor sentence is here.\n")
        self.fn = b / "field-notes"
        self.home = self.fn / "00 Field Notes Home.md"
        write(self.home, "---\ntype: home\nvault_kind: first-hand\n---\n# Home\n\n"
                         "- [[Field Notes - Topic MOC]]\n\n| Case study |\n|---|\n| [[Case Study - Alpha]] |\n")
        self.moc = self.fn / "01 Topic" / "Field Notes - Topic MOC.md"
        write(self.moc, "---\ntype: moc\n---\n# Topic\n\n| Lesson | Where |\n|---|---|\n"
                        "| x | [[Case Study - Alpha#Lesson - Measure Twice]] |\n")
        self.case = self.fn / "10 Case Studies" / "Case Study - Alpha.md"
        write(self.case, page_fm() + case_body())
        self.bench = b / "bench"
        write(self.bench / "00 Bench Home.md", "---\ntype: home\nvault_kind: first-hand\n---\n# Bench\n\n"
                                               "- [[Bench - Tools MOC]]\n- [[Guide - Beta]]\n")
        self.bench_moc = self.bench / "01 Tools" / "Bench - Tools MOC.md"
        write(self.bench_moc, "---\ntype: moc\n---\n# Tools\n\n| Lesson | Where |\n|---|---|\n"
                              "| a | [[Guide - Beta#Lesson - Check The Drainage]] |\n| b | [[Guide - Beta#Trap - Sealed Saucer]] |\n")
        self.guide = self.bench / "01 Tools" / "Guide - Beta.md"
        write(self.guide, page_fm(type="practice-guide") + guide_body())

    def tearDown(self) -> None:
        shutil.rmtree(self.brain, ignore_errors=True)

    def area(self, which="fn") -> dict:
        return R.validate_first_hand_area(self.fn if which == "fn" else self.bench, self.brain)

    def assert_error(self, fragment: str, which="fn") -> None:
        report = self.area(which)
        self.assertTrue([e for e in report["errors"] if fragment in e],
                        f"expected an error containing {fragment!r}; got {report['errors']}")

    def assert_warning(self, fragment: str, which="fn") -> None:
        report = self.area(which)
        self.assertEqual(report["errors"], [], "a warning case must not also produce errors")
        self.assertTrue([w for w in report["warnings"] if fragment in w],
                        f"expected a warning containing {fragment!r}; got {report['warnings']}")

    def set_case(self, fm=None, body=None) -> None:
        write(self.case, (fm if fm is not None else page_fm()) + (body if body is not None else case_body()))

    # positive -------------------------------------------------------------------
    def test_100_clean_area_and_vault(self):
        for which in ("fn", "bench"):
            report = self.area(which)
            self.assertEqual(report["errors"], [], which)
            self.assertEqual(report["warnings"], [], which)
        self.assertEqual(self.area("bench")["lessons"], 2)

    def test_101_kind_is_declared_in_home_frontmatter(self):
        self.assertTrue(R.is_first_hand_area(self.bench))
        self.assertTrue(R.is_first_hand_area(self.fn))
        self.assertFalse(R.is_first_hand_area(self.brain / "pothos"))
        write(self.bench / "00 Bench Home.md", "---\ntype: home\n---\n# Bench\n")
        self.assertFalse(R.is_first_hand_area(self.bench))

    def test_102_brain_dispatches_by_kind(self):
        report = R.validate_brain(self.brain)
        profiles = {v["vault"]: v["profile"] for v in report["vaults"]}
        self.assertEqual(profiles.get("bench"), "first-hand")
        self.assertEqual(profiles.get("field-notes"), "first-hand")
        self.assertNotIn("pothos", profiles)  # no 90 Evidence: not a research vault, as before
        lint = R.lint_brain(self.brain)
        kinds = {v["vault"]: (v["kind"], v["writable"]) for v in lint["vaults"]}
        self.assertEqual(kinds["bench"], ("field", False))

    def test_103_good_callout_passes(self):
        write(self.pothos_note, self.pothos_note.read_text(encoding="utf-8") + "\n"
              + CALLOUT.format(target="Case Study - Alpha", anchor="Lesson - Measure Twice"))
        errors, warnings = R.check_callouts([self.pothos_note], R.BrainIndex(self.brain), str)
        self.assertEqual((errors, warnings), ([], []))

    def test_104_colon_in_anchor_compares_like_obsidian(self):
        heads = R.note_anchors("### Lesson: Measure Twice\n")
        self.assertTrue(R.anchor_resolves("Lesson: Measure Twice", heads))
        self.assertTrue(R.anchor_resolves("Lesson Measure Twice", heads))

    # page frontmatter and headings ------------------------------------------------
    def test_110_missing_page_key(self):
        self.set_case(fm=page_fm(project=None))
        self.assert_error("frontmatter missing 'project'")

    def test_111_empty_artifacts(self):
        self.set_case(fm=page_fm(artifacts="[]"))
        self.assert_error("artifacts must not be empty")

    def test_112_bears_on_must_exist(self):
        self.set_case(fm=page_fm(bears_on='["Ghost Note"]'))
        self.assert_error("bears_on names 'Ghost Note'")

    def test_113_bad_status(self):
        self.set_case(fm=page_fm(status="final"))
        self.assert_error("status must be one of")

    def test_114_superseded_needs_successor(self):
        self.set_case(fm=page_fm(status="superseded"))
        self.assert_error("status superseded needs superseded_by")

    def test_115_missing_case_study_heading(self):
        self.set_case(body=case_body(drop_heading="## What stayed open"))
        self.assert_error("missing body heading '## What stayed open'")

    def test_116_missing_practice_guide_heading(self):
        write(self.guide, page_fm(type="practice-guide") + guide_body(drop_heading="## When to use this"))
        self.assert_error("missing body heading '## When to use this'", "bench")

    # lesson tables ----------------------------------------------------------------
    def test_120_lesson_without_table(self):
        self.set_case(body=case_body(rows=""))
        self.assert_error("no metadata table")

    def test_121_missing_row(self):
        rows = "\n".join(l for l in table().split("\n") if not l.startswith("| Sample"))
        self.set_case(body=case_body(rows=rows + "\n"))
        self.assert_error("lacks the row 'Sample'")

    def test_122_bad_kind(self):
        self.set_case(body=case_body(rows=table(kind="anecdote")))
        self.assert_error("Kind must be one of")

    def test_123_bad_scope(self):
        self.set_case(body=case_body(rows=table(scope="universal truth")))
        self.assert_error("Scope must start with general, domain or project")

    def test_124_bad_grades(self):
        self.set_case(body=case_body(rows=table(grades="measured")))
        self.assert_error("Evidence must be '<number> / <generalization>'")
        self.set_case(body=case_body(rows=table(grades="measured / proven")))
        self.assert_error("Evidence must be '<number> / <generalization>'")

    def test_125_empty_invalidated_by(self):
        self.set_case(body=case_body(rows=table(invalidated="")))
        self.assert_error("'Invalidated by' must not be empty")

    def test_126_empty_supersedes(self):
        self.set_case(body=case_body(rows=table(supersedes="")))
        self.assert_error("'Supersedes' must not be empty")

    def test_127_duplicate_lesson_heading(self):
        self.set_case(body=case_body(extra="\n### Lesson - Measure Twice\n\nAgain.\n\n" + table()))
        self.assert_error("duplicate lesson heading")

    # links and reachability ----------------------------------------------------------
    def test_130_dangling_link(self):
        self.set_case(body=case_body(extra="\nSee [[Nobody Wrote This]].\n"))
        self.assert_error("dangling wikilink [[Nobody Wrote This]]")

    def test_131_dead_anchor_into_another_page(self):
        self.set_case(body=case_body(extra="\nSee [[Guide - Beta#Lesson - Check The Soil]].\n"))
        self.assert_error("anchor [[Guide - Beta#Lesson - Check The Soil]] lands on no heading")

    def test_132_renamed_lesson_breaks_the_moc_row(self):
        self.set_case(body=case_body(lesson="Lesson - Measure Three Times"))
        self.assert_error("anchor [[Case Study - Alpha#Lesson - Measure Twice]] lands on no heading")
        self.assert_error("'Lesson - Measure Three Times' is not linked from any MOC row")

    def test_133_lesson_not_reachable_from_home(self):
        write(self.home, "---\ntype: home\nvault_kind: first-hand\n---\n# Home\n\n- [[Field Notes - Topic MOC]]\n")
        write(self.moc, self.moc.read_text(encoding="utf-8") + "\n[[Case Study - Alpha]]\n")
        self.assert_error("is not reachable from a Home")

    def test_134_moc_prefix_is_derived_from_the_home_name(self):
        self.bench_moc.rename(self.bench_moc.with_name("Tools MOC.md"))
        write(self.bench / "00 Bench Home.md", "---\ntype: home\nvault_kind: first-hand\n---\n# Bench\n\n"
                                               "- [[Tools MOC]]\n- [[Guide - Beta]]\n")
        self.assert_error("MOC basename must start with 'Bench - '", "bench")

    # callouts ------------------------------------------------------------------------
    def callout_report(self, text: str):
        write(self.pothos_note, "---\ntype: concept\n---\n# Pothos Note\n\n" + text)
        return R.check_callouts([self.pothos_note], R.BrainIndex(self.brain), str)

    def test_140_callout_to_missing_page(self):
        errors, _ = self.callout_report(CALLOUT.format(target="Case Study - Gone", anchor="Lesson - Measure Twice"))
        self.assertTrue([e for e in errors if "does not exist" in e], errors)

    def test_141_callout_to_renamed_lesson(self):
        errors, _ = self.callout_report(CALLOUT.format(target="Case Study - Alpha", anchor="Lesson - Old Title"))
        self.assertTrue([e for e in errors if "renamed lesson" in e], errors)

    def test_142_callout_without_details_line(self):
        errors, _ = self.callout_report("> [!example] Tested first-hand\n> Just a number.\n")
        self.assertTrue([e for e in errors if "Details and data" in e], errors)

    def test_143_callout_to_superseded_page_warns(self):
        write(self.fn / "10 Case Studies" / "Case Study - Gamma.md", page_fm() + case_body(title="Case Study - Gamma"))
        self.set_case(fm=page_fm(status="superseded", superseded_by='"Case Study - Gamma"'))
        errors, warnings = self.callout_report(CALLOUT.format(target="Case Study - Alpha", anchor="Lesson - Measure Twice"))
        self.assertEqual(errors, [])
        self.assertTrue([w for w in warnings if "superseded" in w], warnings)

    def test_144_callout_anchor_must_name_a_lesson(self):
        errors, _ = self.callout_report(CALLOUT.format(target="Case Study - Alpha", anchor="What we measured"))
        self.assertTrue([e for e in errors if "must name a 'Lesson - '" in e], errors)

    def test_145_brain_checks_callouts_outside_vaults(self):
        write(self.brain / "inbox" / "Note.md",
              "# D\n\n" + CALLOUT.format(target="Case Study - Alpha", anchor="Lesson - Gone"))
        report = R.validate_brain(self.brain)
        self.assertTrue(report["callouts_outside_vaults"]["errors"])
        self.assertFalse(report["valid"])

    # mixed vault -----------------------------------------------------------------------
    def make_research_vault(self):
        v = self.brain / "res"
        fm = "---\ntype: {t}\nstatus: active\nevidence_level: mixed\npublished: 2026-01-15\nlast_verified: 2026-01-15\nreview_due: 2026-04-15\n{extra}---\n"
        write(v / "00 Res Home.md", fm.format(t="home", extra="") + "# Res\n\n[[01 Res MOC]] [[Case Study - Delta]] [[Res Claim Ledger]]\n")
        write(v / "01 Res" / "01 Res MOC.md", fm.format(t="moc", extra="") + "# MOC\n\n[[Res Note]] [[Case Study - Delta]] [[Case Study - Delta#Lesson - Mixed Lesson]]\n")
        write(v / "01 Res" / "Res Note.md", fm.format(t="concept", extra="claims:\n  - CL-001\n")
              + "# Res Note\n\nA claim (https://example.org). See [[Pothos Note]].\n")
        write(v / "90 Evidence" / "Res Claim Ledger.md", fm.format(t="ledger", extra="") + "# L\n\n| ID | Claim |\n|---|---|\n| CL-001 | x |\n")
        write(v / "01 Res" / "Case Study - Delta.md", page_fm() + case_body(title="Case Study - Delta", lesson="Lesson - Mixed Lesson"))
        return v

    def test_150_mixed_vault_checks_each_note_by_type(self):
        v = self.make_research_vault()
        report = R.validate_vault(v, write_report=False, brain=self.brain)
        self.assertFalse([e for e in report["errors"] if "Case Study - Delta" in e and "evidence_level" in e], report["errors"])
        self.assertEqual(report["first_hand_pages"], 1)
        self.assertEqual(report["errors"], [])

    def test_151_mixed_vault_page_errors_surface(self):
        v = self.make_research_vault()
        write(v / "01 Res" / "Case Study - Delta.md",
              page_fm() + case_body(title="Case Study - Delta", lesson="Lesson - Mixed Lesson", rows=table(kind="bogus")))
        report = R.validate_vault(v, write_report=False, brain=self.brain)
        self.assertTrue([e for e in report["errors"] if "Kind must be one of" in e], report["errors"])

    def test_152_links_resolve_brain_wide_under_a_brain(self):
        v = self.make_research_vault()
        with_brain = R.validate_vault(v, write_report=False, brain=self.brain)
        self.assertFalse([e for e in with_brain["errors"] if "[[Pothos Note]]" in e])
        isolated = R.validate_vault(v, write_report=False, brain=None, _idx=R.BrainIndex(v))
        # without a brain the cross-vault link is unresolved
        self.assertTrue(isolated["errors"])

    # warnings ---------------------------------------------------------------------------
    def test_160_non_english_prose_warns_with_line_numbers(self):
        samples = (
            # two lower-case words with a non-ASCII letter, no stop word
            "Riego cada día después de revisar la tierra húmeda.",
            # three non-English stop words, plain ASCII
            "Die Erde ist trocken und die Pflanze wird morgen gegossen.",
            # a mixed line: English, then one non-ASCII word and one stop word
            "Water the pothos weekly, avec un arrosage léger.",
            # both signals at once
            "La maceta también está junto a la ventana y recibe más luz por la mañana.",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.set_case(body=case_body(extra=f"\n{sample}\n"))
                report = self.area()
                hits = [w for w in report["warnings"] if "non-English prose outside code and quotes" in w]
                self.assertTrue(hits and "line(s)" in hits[0], report["warnings"])

    def test_161_no_false_positive_on_names_paths_code_quotes(self):
        extra = ("\nÅngström met Łódź's team in São Paulo; see `data/sample.json` and data/sample.json.\n"
                 "\n```\ndas ist ein Beispiel und nicht mehr\n```\n"
                 "\n> la plante est arrosée dans une serre\n")
        self.set_case(body=case_body(extra=extra))
        self.assertFalse([w for w in self.area()["warnings"] if "non-English" in w])

    def test_162_long_page_warns(self):
        self.set_case(body=case_body(extra="\n" + "word " * 2900 + "\n"))
        self.assert_warning("words (> 2800)")

    def test_163_measured_generalization_on_one_run_warns(self):
        self.set_case(body=case_body(rows=table(grades="measured / measured", sample="n=1 chain")))
        self.assert_warning("single chain/run")

    def test_164_isolated_lesson_warns(self):
        body = case_body(rows=table(bears="none — nothing in the vaults")).replace("Claim text. See [[Pothos Note]].", "Claim text.")
        self.set_case(body=body)
        self.assert_warning("isolated lesson")

    def test_165_draft_status_warns(self):
        self.set_case(fm=page_fm(status="draft"))
        self.assert_warning("status is draft")

    def test_166_case_study_without_lessons_is_legal(self):
        body = case_body().split("### Lesson - ")[0] + "No lesson on this page; see [[Guide - Beta]].\n\n## What stayed open\n\n- x\n\n## Related\n\n- [[Pothos Note]]\n"
        self.set_case(body=body)
        write(self.moc, "---\ntype: moc\n---\n# Topic\n\nNo lesson rows yet.\n")
        self.assertEqual(self.area()["errors"], [])

    # colon-free headings -------------------------------------------------------------
    def test_170_colon_form_is_read_but_warns(self):
        self.set_case(body=case_body(lesson="Lesson: Measure Twice"))
        write(self.moc, "---\ntype: moc\n---\n# Topic\n\n| Lesson | Where |\n|---|---|\n"
                        "| x | [[Case Study - Alpha#Lesson: Measure Twice]] |\n")
        report = self.area()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["lessons"], 1)
        self.assertTrue([w for w in report["warnings"] if "colon in a linked heading" in w and "'Lesson: Measure Twice'" in w])
        self.assertTrue([w for w in report["warnings"] if "anchor [[Case Study - Alpha#Lesson: Measure Twice]]" in w])

    def test_171_both_forms_are_detected(self):
        text = "---\ntype: case-study\n---\n### Lesson - One\n\nx\n\n### Trap: Two\n\ny\n\n### Lesson -Three\n"
        self.assertEqual([s["heading"] for s in R.lesson_sections(text)], ["Lesson - One", "Trap: Two", "Lesson -Three"])

    def test_172_hyphen_form_is_warning_free(self):
        report = self.area()
        self.assertFalse([w for w in report["warnings"] if "colon" in w])

    def test_173_colon_anchor_in_callout_warns_but_resolves(self):
        self.set_case(body=case_body(lesson="Lesson: Measure Twice"))
        errors, warnings = self.callout_report(CALLOUT.format(target="Case Study - Alpha", anchor="Lesson: Measure Twice"))
        self.assertEqual(errors, [])
        self.assertTrue([w for w in warnings if "colon in a linked heading" in w], warnings)

    def test_174_other_unsafe_characters_in_anchor_warn(self):
        self.set_case(body=case_body(extra="\nSee [[Pothos Note#50% case]].\n"))
        report = self.area()
        self.assertTrue([w for w in report["warnings"] if "may not survive in an Obsidian link" in w], report["warnings"])

    # policy: first-hand pages in research vaults, never evidence ------------------------
    def test_190_first_hand_page_carries_no_claims(self):
        self.set_case(fm=page_fm(claims="[CL-001]"))
        self.assert_error("carries no 'claims:' list")

    def test_191_ledger_row_may_not_cite_a_first_hand_page(self):
        v = self.make_research_vault()
        ledger = v / "90 Evidence" / "Res Claim Ledger.md"
        write(ledger, ledger.read_text(encoding="utf-8").replace("| CL-001 | x |", "| CL-001 | x, see [[Case Study - Delta]] |"))
        report = R.validate_vault(v, write_report=False, brain=self.brain)
        self.assertTrue([e for e in report["errors"] if "row cites first-hand page [[Case Study - Delta]]" in e], report["errors"])

    def test_192_dead_anchor_in_research_vault_names_file_link_and_heading(self):
        v = self.make_research_vault()
        note = v / "01 Res" / "Res Note.md"
        write(note, note.read_text(encoding="utf-8")
              + "\n| a | b |\n|---|---|\n| x | [[Pothos Note#Missing Part\\|shown]] |\n\nSee [[Res Claim Ledger#CL-042]].\n")
        report = R.validate_vault(v, write_report=False, brain=self.brain)
        self.assertIn("01 Res/Res Note.md: link [[Pothos Note#Missing Part]] — no heading 'Missing Part' in Pothos Note",
                      [w.replace("\\", "/") for w in report["warnings"]])
        self.assertTrue([e for e in report["errors"]
                         if "claim anchor [[Res Claim Ledger#CL-042]] — no heading 'CL-042'" in e], report["errors"])

class EscapedPipe(unittest.TestCase):
    """`[[Page#Section\\|text]]` — the escaped alias pipe Obsidian requires in table rows."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="escaped-pipe-"))
        write(self.root / "README.md", "# Brain\n\n## Router\n\n| Vault | When | Entry |\n|---|---|---|\n")
        write(self.root / "v" / "Target Page.md", "---\ntype: concept\n---\n# Target Page\n\n## Section One\n\ntext\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_180_parsers_drop_the_escape(self):
        line = "| a | [[Target Page#Section One\\|shown]] | [[Target Page\\|alias]] |"
        self.assertEqual(R.anchor_links(line), [("Target Page", "Section One")])
        self.assertEqual([m.group(1) for m in R.WIKILINK.finditer(line)], ["Target Page", "Target Page"])
        self.assertEqual(R._prose_links(line), [("Target Page", "Section One"), ("Target Page", "")])

    def test_181_escaped_pipe_anchor_resolves(self):
        page = self.root / "v" / "Linker.md"
        write(page, "# Linker\n\n| a | b |\n|---|---|\n| x | [[Target Page#Section One\\|shown]] |\n")
        errors: list[str] = []
        warnings: list[str] = []
        R.check_links("Linker.md", page, page.read_text(encoding="utf-8"), R.BrainIndex(self.root), errors, warnings)
        self.assertEqual((errors, warnings), ([], []))

    def test_182_escaped_pipe_to_missing_heading_is_reported(self):
        page = self.root / "v" / "Linker.md"
        write(page, "# Linker\n\n| a | b |\n|---|---|\n| x | [[Target Page#Section Two\\|shown]] |\n")
        errors: list[str] = []
        R.check_links("Linker.md", page, page.read_text(encoding="utf-8"), R.BrainIndex(self.root), errors, [])
        self.assertEqual(errors, ["Linker.md: anchor [[Target Page#Section Two]] lands on no heading"])

    def test_183_link_rewrite_round_trip_keeps_the_escape(self):
        row = "| x | [[Target Page#Section One\\|shown]] | [[Target Page\\|alias]] | [[Target Page]] |\n"
        page = self.root / "v" / "Linker.md"
        write(page, "# Linker\n\n| a | b | c |\n|---|---|---|\n" + row)
        before = page.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            report = R.link_rewrite(self.root, {"Target Page": "Renamed Page"}, apply_all=True)
        self.assertEqual(report["rewritten"], 3)
        text = page.read_text(encoding="utf-8")
        self.assertIn("[[Renamed Page#Section One\\|shown]]", text)
        self.assertIn("[[Renamed Page\\|alias]]", text)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            R.link_rewrite(self.root, {"Renamed Page": "Target Page"}, apply_all=True)
        self.assertEqual(page.read_bytes(), before)

    def test_184_callout_details_link_with_escaped_pipe(self):
        write(self.root / "v" / "Case.md", "---\ntype: case-study\n---\n# Case\n\n### Lesson - One\n\nx\n")
        note = self.root / "v" / "Note.md"
        write(note, "# Note\n\n> [!example] Tested first-hand\n> x\n> Details and data: [[Case#Lesson - One\\|the lesson]]\n")
        self.assertEqual(R.check_callouts([note], R.BrainIndex(self.root), str), ([], []))

    def test_185_maintenance_checkers_accept_escaped_pipes(self):
        import subprocess
        import sys as _sys
        write(self.root / "v" / "Linker.md",
              "# Linker\n\n| a | b |\n|---|---|\n| x | [[Target Page#Section One\\|shown]] |\n")
        scripts = SKILL.parent / "maintenance"
        env = dict(__import__("os").environ, PYTHONIOENCODING="utf-8")
        done = subprocess.run([_sys.executable, str(scripts / "check_anchors.py"), str(self.root)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        out_json = self.root / "audit.json"
        subprocess.run([_sys.executable, str(scripts / "anchor_audit.py"), str(self.root), str(out_json)],
                       capture_output=True, text=True, env=env, check=True)
        audit = json.loads(out_json.read_text(encoding="utf-8"))
        dead = sum(v.get("other_dead", 0) + v.get("cl_dead", 0) for v in audit.values() if isinstance(v, dict))
        self.assertEqual(dead, 0, audit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
