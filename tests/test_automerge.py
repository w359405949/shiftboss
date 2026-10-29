"""Managed merges must outlive the interactive session that launched patrol."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
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
                self.assertEqual(len(calls), 2)
                self.assertIn("--done", calls[1][0])
                for _, environment in calls:
                    for key in inherited.keys() - {"PATH"}:
                        self.assertNotIn(key, environment)
                    self.assertEqual(environment["PATH"], "preserved-path")
                    self.assertEqual(environment[AUTOMERGE_ENV], "1")
                self.assertEqual(state.current_session_id(), "old-boss")
                with self.assertRaisesRegex(SystemExit, "本会话不是派活方"):
                    state.require_dispatcher()
                self.assertEqual(state.read_state(directory / state.DISPATCHER_NAME)["session"], "current-boss")
