import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCHERS = ("lifeline", "lifeline-statusline", "lifeline-signin-page")


def copy_layout(root):
    shutil.copytree(REPO / "core" / "lifeline", root / "core" / "lifeline", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(REPO / "bin", root / "bin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return root


def caches(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.name == "__pycache__" or path.suffix == ".pyc")


class LauncherBytecodeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
            "LANG": "C",
            "LIFELINE_SOCKET": str(self.root / "no.sock"),
        }
        env.update(extra)
        return env

    def run_launcher(self, copy, name, args=(), stdin="", **extra):
        return subprocess.run(
            [str(copy / "bin" / name), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(**extra),
            cwd=str(self.root),
            timeout=60,
        )

    def runs(self, copy, run):
        done = run(copy, "lifeline", ("help",))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("lifeline", done.stdout)
        done = run(copy, "lifeline-statusline", stdin=json.dumps({"session_id": "s1"}))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        page = self.root / ("page-" + copy.name)
        done = run(copy, "lifeline-signin-page", ("https://claude.com/cai/oauth/authorize?x=1",), LIFELINE_SIGNIN_PAGE=str(page))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(page.read_text(), "https://claude.com/cai/oauth/authorize?x=1\n")

    def test_every_launcher_runs_without_writing_bytecode_beside_the_code(self):
        copy = copy_layout(self.root / "copy")
        self.assertEqual(caches(copy), [])
        self.runs(copy, self.run_launcher)
        self.assertEqual(caches(copy), [])

    def test_the_same_runs_without_the_flag_would_write_bytecode(self):
        copy = copy_layout(self.root / "control")

        def without_flag(copy, name, args=(), stdin="", **extra):
            return subprocess.run(
                [sys.executable, "-I", str(copy / "bin" / name), *args],
                input=stdin,
                capture_output=True,
                text=True,
                env=self.env(**extra),
                cwd=str(self.root),
                timeout=60,
            )

        self.runs(copy, without_flag)
        self.assertTrue(caches(copy))

    def test_every_launcher_asks_for_no_bytecode_and_keeps_isolated_mode(self):
        for name in LAUNCHERS:
            with self.subTest(name=name):
                first = (REPO / "bin" / name).read_text().splitlines()[0]
                self.assertEqual(first, "#!/usr/bin/env -S python3 -I -B")


if __name__ == "__main__":
    unittest.main()
