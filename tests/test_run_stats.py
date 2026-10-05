"""Tests for skills/<name>/scripts/maintenance/run_stats.py: counts are right, and nothing
identifying (run id, question, URL, title, path) ever reaches the output.

Run from the repo root:  python -m unittest -v tests.test_run_stats
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

# The skill lives in skills/<name>/; found by its SKILL.md so a rename needs no edit here.
SCRIPT = next((Path(__file__).resolve().parent.parent / "skills").glob("*/SKILL.md")).parent / "scripts" / "maintenance" / "run_stats.py"
_spec = importlib.util.spec_from_file_location("run_stats", SCRIPT)
S = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(S)

RUN_ID = "secret-topic-zebra-quokka-1a2b3c4d"
QUESTION = "What does the private zebra quokka project need?"
URL = "https://private.example.org/quokka/report"
TITLE = "Quokka Internal Report"


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_run(root: Path, run_id: str, question: str, claim_statuses: list[str]) -> None:
    run = root / run_id
    _write(run / "manifest.json", {"run_id": run_id, "question": question, "kind": "research",
                                   "mode": "small", "created_at": "2026-01-01T00:00:00Z"})
    _write(run / "taxonomy.json", {"domains": [{"id": "D1", "name": "Quokka habits"},
                                               {"id": "D2", "name": "Zebra habits"},
                                               {"id": "D3", "name": "Limits"}]})
    _write(run / "records" / "sources.json",
           {"S-001": {"id": "S-001", "url": URL, "canonical_url": URL, "title": TITLE, "origin": "new"}})
    _write(run / "records" / "claims.json",
           {f"CL-{i:03d}": {"id": f"CL-{i:03d}", "status": s, "origin": "new", "text": question}
            for i, s in enumerate(claim_statuses, 1)})
    _write(run / "records" / "excerpts.json",
           {"X-001": {"id": "X-001", "quote": TITLE, "verification": {"status": "exact"}},
            "X-002": {"id": "X-002", "quote": TITLE, "verification": {"status": "mismatch"}},
            "X-003": {"id": "X-003", "quote": TITLE}})
    _write(run / "records" / "notes.json", {"N-001": {"id": "N-001", "title": TITLE}})
    _write(run / "reports" / "handbacks.json",
           [{"status": "capped", "unread": [{"url": URL + "/2", "why_it_matters": question}]}])


class RunStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="run-stats-"))
        # A free-text status (equal to the question) must be bucketed, not echoed.
        _make_run(self.root, RUN_ID, QUESTION, ["supported", "qualified", "unsupported", QUESTION])
        (self.root / "ACTIVE.json").write_text("{}", encoding="utf-8")  # a file, not a run

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = S.main(["--runs-root", str(self.root), *extra])
        return code, out.getvalue()

    def test_counts(self) -> None:
        code, text = self._run("--json")
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["domains_per_run"]["min"], 3)
        self.assertEqual(report["totals"], {"sources": 1, "claims": 4, "excerpts": 3, "notes": 1})
        self.assertEqual(report["claim_status"]["other"]["n"], 1)
        self.assertEqual(report["excerpt_quote_check"]["unchecked"]["n"], 1)
        self.assertEqual(report["handback_status"], {"capped": 1})

    def test_no_identifying_strings_in_any_output(self) -> None:
        for extra in ((), ("--json",)):
            code, text = self._run(*extra)
            self.assertEqual(code, 0)
            for secret in (RUN_ID, QUESTION, URL, TITLE, "quokka", "zebra", "://", str(self.root)):
                self.assertNotIn(secret.lower(), text.lower(), f"{secret!r} leaked with {extra}")

    def test_leak_guard_refuses_to_print(self) -> None:
        forbidden = {RUN_ID}
        self.assertEqual(S.leaks(f"run {RUN_ID}", forbidden), 1)
        self.assertEqual(S.leaks("see https://x", set()), 1)
        self.assertEqual(S.leaks("runs 1", forbidden), 0)


if __name__ == "__main__":
    unittest.main()
