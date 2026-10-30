"""Idle follow-up must work when brief is a child of the bell listener."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import brief, idle_audit
from shiftboss.state import write_state


class IdleFollowUp(unittest.TestCase):
    def test_archived_red_pages_do_not_block_an_otherwise_idle_brief(self):
        idle = ("【该你处理】：无\n【待你定】：无\n"
                "【在跑】0/10 会话：无\n【排队】0：无\n"
                "- 页面红 6｜已归档｜sweep-cutscene-lock-readiness\n")
        result = subprocess.CompletedProcess([], 0, stdout=idle)
        with patch.object(idle_audit.subprocess, "run", return_value=result):
            self.assertEqual(idle_audit.idle_now(Path(".")), (True, ""))

    def test_actionable_red_or_active_repair_still_blocks_idle(self):
        idle = ("【该你处理】：无\n【待你定】：无\n"
                "【在跑】0/10 会话：无\n【排队】0：无\n")
        for old, new in [("【该你处理】：无", "【该你处理】工具闸红无人负责"),
                         ("【在跑】0/", "【在跑】1/"),
                         ("【排队】0", "【排队】1")]:
            with self.subTest(new=new):
                result = subprocess.CompletedProcess([], 0, stdout=idle.replace(old, new))
                with patch.object(idle_audit.subprocess, "run", return_value=result):
                    self.assertFalse(idle_audit.idle_now(Path("."))[0])

    def test_listener_can_observe_idle_and_propose_after_two_bells(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "project"
            remote = root / "project.shiftboss" / "hub.git"
            repo.mkdir()
            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
            git("init", "-q", "-b", "main")
            git("init", "--bare", "-q", str(remote))
            git("remote", "add", "origin", str(remote))
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "--allow-empty", "-qm", "idle fixture")
            git("push", "-q", "origin", "main")
            (repo / "shiftboss.toml").write_text("[automation]\nidle_audit=true\n")
            (repo / "shiftboss" / "plans").mkdir(parents=True)
            # A leftover directory is not an active taskbook and must not suppress the bell.
            (repo / "shiftboss" / "plans" / "sweep-project-audit-old").mkdir()
            control = remote.parent / "control"
            control.mkdir(exist_ok=True)
            bell = control / "bell.log"
            bell.touch()
            script = '''
import json
from pathlib import Path
from unittest.mock import patch
from shiftboss import doorbell, idle_audit
from shiftboss.state import read_state
repo = Path.cwd()
remote = repo.parent / "project.shiftboss" / "hub.git"
bell = remote.parent / "control" / "bell.log"
assert doorbell.rival_listeners(bell, bell) == [], "replacement must never target its own process"
assert doorbell.listening(bell), "status must count its own listener"
assert idle_audit.idle_now(repo) == (True, "")
with patch.object(idle_audit.time, "time", return_value=1000):
    first = idle_audit.observe(remote)
with patch.object(idle_audit.time, "time", return_value=2801):
    second = idle_audit.observe(remote)
assert first is None
assert second and second.startswith("idle-audit ")
state = read_state(idle_audit.state_path(remote))
assert state["proposed_head"]
assert idle_audit.pending(repo, remote) == state["proposed_head"]
assert idle_audit.observe(remote) is None, "same head must not emit another proposal"
print(json.dumps({"first": first, "second": second}))
'''
            result = subprocess.run([sys.executable, "-c", script, "shiftboss.doorbell", "--path", str(bell)],
                                    cwd=repo, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsNotNone(json.loads(result.stdout)["second"])

    def test_pending_audit_remains_actionable_until_started(self):
        with tempfile.TemporaryDirectory() as temp:
            control = Path(temp)
            write_state(control / "idle-audit.json", {"proposed_head": "a" * 40, "audited_head": "b" * 40})
            with (patch.object(idle_audit.config, "control_dir", return_value=control),
                  patch.object(idle_audit.config, "automation", return_value=True),
                  patch.object(idle_audit, "audit_in_progress", return_value=False)):
                head = idle_audit.pending(control, control)
                items = brief.actionable([], [], [], [], [], [], [], audit=head)
                self.assertTrue(any("闲时全项目审查" in item for item in items))
                with patch.object(idle_audit, "audit_in_progress", return_value=True):
                    self.assertIsNone(idle_audit.pending(control, control))
                with patch.object(idle_audit.config, "automation", return_value=False):
                    self.assertIsNone(idle_audit.pending(control, control))


if __name__ == "__main__":
    unittest.main()
