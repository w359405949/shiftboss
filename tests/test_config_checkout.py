"""常驻进程跨 checkout 读取实际配置；不执行产品命令。"""
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from shiftboss import config


class ConfigCheckout(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / config.CONFIG_NAME
        self.addCleanup(config.load.cache_clear)

    def test_checkout_replaces_command_and_automation_in_same_process(self):
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()
        git("init", "-q")
        git("config", "user.name", "Config fixture")
        git("config", "user.email", "fixture@localhost")
        self.path.write_text('[regen]\nclosure_command=["node", "old.mjs"]\n[automation]\nverify=true\n')
        git("add", ".")
        git("commit", "-qm", "old")
        old = git("rev-parse", "HEAD")
        self.assertEqual(config.closure_command(self.root), ["node", "old.mjs"])
        self.assertTrue(config.automation("verify", self.root))
        self.path.write_text('[regen]\nclosure_command=["node", "--import", "register.mjs", "new.mjs"]\n[automation]\nverify=false\n')
        git("add", ".")
        git("commit", "-qm", "new")
        new = git("rev-parse", "HEAD")
        git("checkout", "-q", old)
        self.assertEqual(config.closure_command(self.root), ["node", "old.mjs"])
        git("checkout", "-q", new)
        self.assertEqual(config.closure_command(self.root), ["node", "--import", "register.mjs", "new.mjs"])
        self.assertFalse(config.automation("verify", self.root))

    def test_equal_size_and_mtime_do_not_hide_content_change(self):
        self.path.write_text('[regen]\nplan="one.json"\n')
        before = self.path.stat()
        self.assertEqual(config.plan_file(self.root), "one.json")
        self.path.write_text('[regen]\nplan="two.json"\n')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(config.plan_file(self.root), "two.json")

    def test_missing_created_deleted_and_invalid_are_not_cached(self):
        self.assertEqual(config.load(self.root), {})
        self.path.write_text('[regen]\nplan="present.json"\n')
        self.assertEqual(config.plan_file(self.root), "present.json")
        self.path.write_text("[invalid")
        with self.assertRaises(tomllib.TOMLDecodeError):
            config.load(self.root)
        self.path.unlink()
        self.assertEqual(config.load(self.root), {})

    def test_implicit_root_is_resolved_on_each_read(self):
        second = self.root / "second"
        second.mkdir()
        self.path.write_text('[regen]\nplan="one.json"\n')
        (second / config.CONFIG_NAME).write_text('[regen]\nplan="two.json"\n')
        with patch("shiftboss.config.project_root", side_effect=[self.root, second]):
            self.assertEqual(config.plan_file(), "one.json")
            self.assertEqual(config.plan_file(), "two.json")


if __name__ == "__main__":
    unittest.main()
