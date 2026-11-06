"""重验跨过沿用报告的实际来源，不运行产品。"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import verify


class RerunSource(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reports = self.root / "reports"
        (self.reports / "runs").mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@localhost")
        self.chain = []
        for index in range(4):
            (self.root / "source.txt").write_text(str(index))
            self.git("add", "source.txt")
            self.git("commit", "-qm", str(index))
            self.chain.append(self.git("rev-parse", "HEAD"))
        self.addCleanup(patch.stopall)
        patch("shiftboss.verify.state_dir", return_value=self.reports).start()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def write(self, relative, data):
        (self.reports / relative).write_text(json.dumps(data))

    def test_skips_all_carried_reports_and_actual_failed_source(self):
        first, failed, carried, tip = self.chain
        for sha, data in [(first, {}), (failed, {"failures": ["infrastructure"]}),
                          (carried, {"carried_from": failed}), (tip, {"carried_from": carried})]:
            self.write(f"runs/{sha}.json", data)
        self.write("index.json", {"schema": verify.SCHEMA, "last": tip, "last_consistent": first})
        self.assertEqual(verify.rerun_tip(self.root), 0)
        index = json.loads((self.reports / "index.json").read_text())
        self.assertEqual(index["last"], first)
        self.assertEqual(index["last_consistent"], first)
        self.assertEqual(len(list((self.reports / "runs").iterdir())), 4)

    def test_broken_or_cyclic_source_does_not_change_index(self):
        tip = self.chain[-1]
        self.write("index.json", {"schema": verify.SCHEMA, "last": tip})
        before = (self.reports / "index.json").read_bytes()
        for source in ["missing", tip]:
            with self.subTest(source=source):
                self.write(f"runs/{tip}.json", {"carried_from": source})
                with self.assertRaises(ValueError):
                    verify.rerun_tip(self.root)
                self.assertEqual((self.reports / "index.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
