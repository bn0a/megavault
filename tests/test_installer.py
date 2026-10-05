"""Tests for the npm installer (`installer/cli.js`): install, --force and uninstall
touch only the files the package ships. Skipped when Node.js is not on PATH.

Every install goes into a throwaway config directory under the system temp dir,
never into a real Claude Code config.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "installer" / "cli.js"
SKILL = next((REPO / "skills").glob("*/SKILL.md")).parent
NODE = shutil.which("node")
HOSTNAME_EXE = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "hostname.exe"


@unittest.skipUnless(NODE, "Node.js is not on PATH")
class InstallerTouchesOnlyItsOwnFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.claude = Path(tempfile.mkdtemp(prefix="megavault-installer-"))
        self.addCleanup(shutil.rmtree, self.claude, True)
        self.skill = self.claude / "skills" / SKILL.name
        self.agents = self.claude / "agents"

    def cli(self, *args: str, expect: int = 0) -> str:
        result = subprocess.run(
            [NODE, str(CLI), *args, "--claude-dir", str(self.claude)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stdout

    def shipped(self) -> list[Path]:
        files = [p for p in SKILL.rglob("*") if p.is_file()
                 and "__pycache__" not in p.parts and p.suffix != ".pyc"]
        return [self.skill / p.relative_to(SKILL) for p in files]

    def test_1_install_copies_every_shipped_file(self) -> None:
        self.cli("install")
        missing = [p for p in self.shipped() if not p.is_file()]
        self.assertEqual(missing, [])
        self.assertEqual(len(list(self.agents.glob("research-*.md"))), 4)
        self.cli("install", expect=1)  # refuses to overwrite without --force

    def test_2_force_and_uninstall_keep_user_files(self) -> None:
        self.cli("install")
        mine = self.skill / "scripts" / "my_profiles_backup.json"
        mine.write_text("{}", encoding="utf-8")
        foreign_agent = self.agents / "someone-else.md"
        foreign_agent.write_text("not ours", encoding="utf-8")
        bytecode = self.skill / "scripts" / "__pycache__" / "research.cpython-310.pyc"
        bytecode.parent.mkdir()
        bytecode.write_bytes(b"\0")

        out = self.cli("install", "--force")
        self.assertTrue(mine.is_file(), "--force kept a file the package does not ship")
        self.assertIn("my_profiles_backup.json", out)

        self.cli("uninstall", "--dry-run")
        self.assertTrue((self.skill / "SKILL.md").is_file(), "a dry run removes nothing")

        out = self.cli("uninstall")
        self.assertTrue(mine.is_file(), "uninstall kept a file the package did not install")
        self.assertIn("my_profiles_backup.json", out)
        self.assertTrue(foreign_agent.is_file(), "uninstall kept a foreign agent file")
        self.assertFalse(bytecode.exists(), "bytecode of the package's scripts is removed")
        self.assertEqual([p for p in self.shipped() if p.exists()], [])
        self.assertEqual(list(self.agents.glob("research-*.md")), [])
        self.assertEqual(sorted(p.name for p in self.skill.rglob("*")),
                         ["my_profiles_backup.json", "scripts"])

        mine.unlink()
        self.cli("install")
        self.cli("uninstall")
        self.assertFalse(self.skill.exists(), "an install with no extras leaves nothing behind")

    def test_3_python_check_ignores_a_planted_python_in_the_working_directory(self) -> None:
        # npx runs the installer from wherever the user stands. On Windows, older
        # libuv looks a bare `python` up in the child's working directory before
        # PATH, so the version check runs from the package directory, which ships
        # no program.
        source = CLI.read_text(encoding="utf-8")
        self.assertRegex(source, r"spawnSync\(cmd, \['--version'\], \{[^}]*cwd: PKG_ROOT",
                         "the version check pins its working directory")
        if not (os.name == "nt" and HOSTNAME_EXE.is_file() and shutil.which("python")):
            return
        here = Path(tempfile.mkdtemp(prefix="megavault-cwd-"))
        self.addCleanup(shutil.rmtree, here, True)
        for name in ("python.exe", "python3.exe"):
            shutil.copyfile(HOSTNAME_EXE, here / name)  # any program that is not Python
        result = subprocess.run(
            [NODE, str(CLI), "install", "--claude-dir", str(self.claude)],
            capture_output=True, text=True, timeout=60, cwd=here,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("python: Python 3.", result.stdout, "the real Python answered, not the planted one")

    def test_4_help_names_the_command_that_runs_this_package(self) -> None:
        # The package is not on the npm registry: `npx <name>` would run whatever
        # registry package holds that name, so usage names the GitHub form.
        result = subprocess.run([NODE, str(CLI), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npx github:bn0a/megavault [install]", result.stdout)
        self.assertNotRegex(result.stdout, r"npx megavault\b")
        refused = subprocess.run([NODE, str(CLI), "--no-such-option"], capture_output=True, text=True, timeout=60)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("npx github:bn0a/megavault --help", refused.stderr)
        self.assertNotRegex(refused.stderr, r"run 'megavault --help'")


if __name__ == "__main__":
    unittest.main()
