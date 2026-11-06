"""临时 Git 检出销毁后，仍可读到首个错误及完整阶段日志。"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import config, regen


class BranchCheckDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = subprocess.run
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Diagnostic fixture")
        self.git("config", "user.email", "fixture@localhost")
        (self.root / "shiftboss.toml").write_text('[regen]\nplan = "regen.json"\n')
        self.plan = {"stages": [{"name": "fixture", "command": ["python", "-B", "-m", "fixture"],
                                 "inputs": ["source/"]}]}
        (self.root / "regen.json").write_text(json.dumps(self.plan))
        (self.root / "source").mkdir()
        (self.root / "source/input").write_text("before")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        (self.root / "source/input").write_text("after")
        self.git("commit", "-qam", "change")
        (self.root / "out").mkdir()
        self.addCleanup(config.load.cache_clear)

    def git(self, *args):
        return self.run(["git", *args], cwd=self.root, check=True,
                        capture_output=True, text=True).stdout.strip()

    def check(self, code):
        calls = []
        def run(command, **kwargs):
            if command[0] == "git":
                return self.run(command, **kwargs)
            calls.append((command, Path(kwargs["cwd"])))
            # 小型真实子进程，写输出和阶段日志；不执行项目生成。
            script = (
                "from pathlib import Path\nimport sys\n"
                "p=Path('out/regen-failures/1-fixture.log')\n"
                "p.parent.mkdir(parents=True)\n"
                "p.write_text('ORIGINAL-STAGE-ERROR\\n' + 'detail\\n'*80)\n"
                "print('FIRST-STDOUT-ERROR')\n"
                "print('progress\\n'*70, end='')\n"
                "print('ORIGINAL-STDERR-ERROR', file=sys.stderr)\n"
                f"sys.exit({code})\n")
            return self.run([sys.executable, "-c", script], **kwargs)
        printed = io.StringIO()
        with (patch.object(regen, "REPO", self.root),
              patch.object(regen.subprocess, "run", side_effect=run),
              patch.object(regen, "closure_stages", side_effect=lambda stages: stages),
              patch.object(regen, "checks_dir", return_value=self.root / "checks"),
              contextlib.redirect_stdout(printed)):
            result = regen.branch_check("HEAD")
        head = self.git("rev-parse", "HEAD")
        record = json.loads((self.root / "checks" / f"{head}.json").read_text())
        self.assertEqual(len(calls), 1)
        command, worktree = calls[0]
        self.assertFalse(worktree.exists())
        self.assertEqual(record["diagnostics"]["command"], command)
        self.assertEqual(record["diagnostics"]["cwd"], str(worktree))
        self.assertEqual(command[5:], ["--stage", "fixture"])
        return result, record, printed.getvalue()

    def test_failed_child_retains_first_error_and_stage_log_after_cleanup(self):
        result, record, terminal = self.check(7)
        self.assertEqual(result, 1)
        self.assertFalse(record["ok"])
        self.assertEqual(record["returncode"], 7)
        diagnostics = record["diagnostics"]
        self.assertEqual(diagnostics["returncode"], 7)
        self.assertNotIn("FIRST-STDOUT-ERROR", terminal)
        self.assertIn("完整回显：", terminal)
        self.assertIn("FIRST-STDOUT-ERROR", Path(diagnostics["stdout"]).read_text())
        self.assertEqual(Path(diagnostics["stderr"]).read_text(), "ORIGINAL-STDERR-ERROR\n")
        self.assertIn("FIRST-STDOUT-ERROR", Path(diagnostics["output"]).read_text())
        self.assertEqual(len(diagnostics["failure_logs"]), 1)
        failure = diagnostics["failure_logs"][0]
        self.assertFalse(Path(failure["original"]).exists())
        self.assertEqual(Path(failure["retained"]).read_text(),
                         "ORIGINAL-STAGE-ERROR\n" + "detail\n" * 80)
        manifest = json.loads((Path(diagnostics["directory"]) / "command.json").read_text())
        self.assertEqual(manifest["head"], record["head"])
        self.assertEqual(manifest["returncode"], 7)

    def test_success_preserves_actual_returncode_and_distinct_run_paths(self):
        result, first, _ = self.check(0)
        self.assertEqual(result, 0)
        self.assertTrue(first["ok"])
        self.assertEqual(first["returncode"], 0)
        result, second, _ = self.check(0)
        self.assertEqual(result, 0)
        self.assertNotEqual(first["diagnostics"]["directory"], second["diagnostics"]["directory"])
        self.assertTrue(Path(first["diagnostics"]["stdout"]).is_file())
        self.assertTrue(Path(second["diagnostics"]["stdout"]).is_file())


if __name__ == "__main__":
    unittest.main()
