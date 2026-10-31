"""Managed merges must outlive the interactive session that launched patrol."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from shiftboss import automerge, state
from shiftboss.merge import AUTOMERGE_ENV


class BackgroundMergeIdentity(unittest.TestCase):
    def test_merge_and_archive_do_not_inherit_stale_dispatcher_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            state.write_state(directory / state.DISPATCHER_NAME, {"session": "current-boss"})
            inherited = {key: "old-boss" for key in (
                "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID")}
            inherited["PATH"] = "preserved-path"
            calls = []

            def child(args, **kwargs):
                environment = kwargs.get("env", dict(os.environ))
                with patch.dict(os.environ, environment, clear=True):
                    state.require_dispatcher()
                calls.append((args, environment))
                return subprocess.CompletedProcess(args, 0, "", "")

            with (patch.dict(os.environ, inherited, clear=True),
                  patch("shiftboss.config.control_dir", return_value=directory),
                  patch("shiftboss.inbox.origin_git_dir"),
                  patch("shiftboss.paths.project_root"),
                  patch.object(automerge, "git"),
                  patch.object(automerge, "subtypes", return_value=["repair"]),
                  patch.object(automerge, "read_state", return_value={}),
                  patch.object(automerge, "candidates", return_value=[("repair-one", "a" * 40)]),
                  patch.object(automerge, "push"),
                  patch.object(automerge, "record"),
                  patch.object(automerge.subprocess, "run", side_effect=child)):
                with self.assertRaisesRegex(SystemExit, "本会话不是派活方"):
                    state.require_dispatcher()
                automerge.run(directory / "hub.git", directory / "managed", lambda *args: None)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("--done", calls[0][0])
                for _, environment in calls:
                    for key in inherited.keys() - {"PATH"}:
                        self.assertNotIn(key, environment)
                    self.assertEqual(environment["PATH"], "preserved-path")
                    self.assertEqual(environment[AUTOMERGE_ENV], "1")
                self.assertEqual(state.current_session_id(), "old-boss")
                with self.assertRaisesRegex(SystemExit, "本会话不是派活方"):
                    state.require_dispatcher()
                self.assertEqual(state.read_state(directory / state.DISPATCHER_NAME)["session"], "current-boss")


class EvidenceRetention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "project"
        self.repo.mkdir()
        self.remote = Path(self.temp.name) / "hub.git"
        self.control = Path(self.temp.name) / "control"
        self.control.mkdir()
        patcher = patch("shiftboss.config.control_dir", return_value=self.control)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.name = "repair-one"
        self.tip = "a" * 40
        automerge.record(self.remote, "merged", self.name,
                         {"tip": self.tip, "acked": False})
        self.raw = self.repo / "out" / self.name / "runtime.json"
        self.raw.parent.mkdir(parents=True)
        self.raw.write_text("original evidence")
        self.book = SimpleNamespace(path=self.repo / "shiftboss" / "plans" / self.name)

    def test_background_merge_keeps_evidence_until_explicit_ack(self):
        def child(args, **kwargs):
            if "--done" in args:
                self.raw.unlink()
            return subprocess.CompletedProcess(args, 0, "", "")

        with (patch.object(automerge, "git"),
              patch.object(automerge, "subtypes", return_value=["repair"]),
              patch.object(automerge, "candidates", return_value=[(self.name, self.tip)]),
              patch.object(automerge, "push"),
              patch.object(automerge.subprocess, "run", side_effect=child)):
            automerge.run(self.remote, self.repo, lambda *args: None)
        self.assertEqual(self.raw.read_text(), "original evidence")
        self.assertEqual(automerge.unreviewed(self.remote), [self.name])
        with (patch("shiftboss.taskbook.Taskbook.find", return_value=self.book),
              patch.object(automerge.subprocess, "run", side_effect=child),
              patch.object(automerge, "push") as push):
            automerge.acknowledge(self.repo, self.remote, self.name)
        self.assertFalse(self.raw.exists())
        self.assertEqual(automerge.unreviewed(self.remote), [])
        push.assert_called_once_with(self.repo)

    def test_failed_archive_keeps_evidence_and_review_pending(self):
        failure = subprocess.CompletedProcess([], 1, "", "archive failed")
        with (patch("shiftboss.taskbook.Taskbook.find", return_value=self.book),
              patch.object(automerge.subprocess, "run", return_value=failure),
              patch.object(automerge, "push") as push):
            with self.assertRaisesRegex(SystemExit, "archive failed"):
                automerge.acknowledge(self.repo, self.remote, self.name)
        self.assertTrue(self.raw.exists())
        self.assertEqual(automerge.unreviewed(self.remote), [self.name])
        push.assert_not_called()

    def test_failed_push_can_retry_already_archived_book(self):
        def archive(args, **kwargs):
            self.raw.unlink()
            self.book.path = self.repo / "shiftboss" / "plans" / "archive" / self.name
            return subprocess.CompletedProcess(args, 0, "", "")

        with (patch("shiftboss.taskbook.Taskbook.find", return_value=self.book),
              patch.object(automerge.subprocess, "run", side_effect=archive) as child,
              patch.object(automerge, "push", side_effect=RuntimeError("push failed"))):
            with self.assertRaisesRegex(RuntimeError, "push failed"):
                automerge.acknowledge(self.repo, self.remote, self.name)
        self.assertEqual(automerge.unreviewed(self.remote), [self.name])
        child.assert_called_once()
        with (patch("shiftboss.taskbook.Taskbook.find", return_value=self.book),
              patch.object(automerge.subprocess, "run") as child,
              patch.object(automerge, "push")):
            automerge.acknowledge(self.repo, self.remote, self.name)
        child.assert_not_called()
        self.assertEqual(automerge.unreviewed(self.remote), [])

    def test_changed_merge_record_is_not_acknowledged(self):
        self.book.path = self.repo / "shiftboss" / "plans" / "archive" / self.name
        def push(repo):
            automerge.record(self.remote, "merged", self.name,
                             {"tip": "b" * 40, "acked": False})
        with (patch("shiftboss.taskbook.Taskbook.find", return_value=self.book),
              patch.object(automerge, "push", side_effect=push)):
            with self.assertRaisesRegex(SystemExit, "记录已变化"):
                automerge.acknowledge(self.repo, self.remote, self.name)
        self.assertEqual(automerge.unreviewed(self.remote), [self.name])
