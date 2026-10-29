"""执行方插件接口与平台脚本。"""
from pathlib import Path
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from shiftboss import dispatch
from shiftboss import native
from shiftboss import state
from shiftboss.workers import WORKERS
from shiftboss.workers import codex

INTERFACE = ("PROCESS_NAMES", "CAN_QUEUE", "SESSION_WAIT", "tui_command", "configured",
             "newest_session", "step_count", "parked_at", "model_of", "queue_into",
             "sandbox_warning", "prepare")


class Workers(unittest.TestCase):
    def test_codex_new_and_resumed_workers_do_not_share_automatic_memory(self):
        with patch.object(codex, "resolve_codex", return_value=Path("/bin/codex")):
            for resume_id in (None, "worker-session"):
                command = codex.tui_command("/tmp/worker", "按合同交付", resume_id=resume_id)
                disabled = [command[i + 1] for i, arg in enumerate(command) if arg == "--disable"]
                overrides = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
                self.assertIn("memories", disabled)
                self.assertIn("external_agent_memory_import", disabled)
                self.assertIn("memories.use_memories=false", overrides)
                self.assertIn("memories.generate_memories=false", overrides)
                self.assertEqual(command[-3:] if resume_id else command[-1:],
                                 ["resume", resume_id, "按合同交付"] if resume_id else ["按合同交付"])

    def test_builtin_workers_implement_interface(self):
        self.assertEqual(set(WORKERS) >= {"codex", "agy", "omp"}, True)
        for name, worker in WORKERS.items():
            missing = [attr for attr in INTERFACE if not hasattr(worker, attr)]
            self.assertEqual(missing, [], name)

    def test_executor_of_falls_back_to_default(self):
        self.assertEqual(dispatch.executor_of("agy (gemini)"), "agy")
        self.assertEqual(dispatch.executor_of("OMP"), "omp")
        self.assertEqual(dispatch.executor_of(None), dispatch.default_worker())
        self.assertEqual(dispatch.executor_of("未知执行方"), dispatch.default_worker())

    def test_workers_without_queue_never_claim_delivery(self):
        for name, worker in WORKERS.items():
            if not worker.CAN_QUEUE:
                self.assertFalse(worker.queue_into("no-such-session", "继续", lambda: True), name)


class DispatcherIdentity(unittest.TestCase):
    def test_codex_worker_cannot_use_dispatcher_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            state.write_state(directory / state.DISPATCHER_NAME, {"session": "boss-session"})
            identities = [{key: "worker-session"}
                          for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID")]
            identities.append({"CODEX_THREAD_ID": "worker-session", "CLAUDE_CODE_SESSION_ID": "boss-session"})
            for identity in identities:
                with (patch.dict(os.environ, identity, clear=True),
                      patch("shiftboss.config.control_dir", return_value=directory),
                      patch("shiftboss.inbox.origin_git_dir"), patch("shiftboss.paths.project_root")):
                    with self.assertRaisesRegex(SystemExit, "本会话不是派活方"):
                        state.require_dispatcher()

    def test_registered_codex_dispatcher_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            state.write_state(directory / state.DISPATCHER_NAME, {"session": "boss-session"})
            with (patch.dict(os.environ, {"CODEX_THREAD_ID": "boss-session"}, clear=True),
                  patch("shiftboss.config.control_dir", return_value=directory),
                  patch("shiftboss.inbox.origin_git_dir"), patch("shiftboss.paths.project_root")):
                state.require_dispatcher()


class Native(unittest.TestCase):
    def test_spawn_passes_argv_verbatim(self):
        """参数经文件传入，不经命令行拼接：空格、引号、反斜杠、换行、中文原样到达。"""
        tricky = ["with space", 'projects={"E:\\MM Editor\\x"={trust_level="trusted"}}',
                  "trail\\", "中文 \"引号\"", "line1\nline2"]
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "argv.json"
            script = ("import json, sys; from pathlib import Path; "
                      f"Path({str(out)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')")
            pid = native.spawn([sys.executable, "-c", script, *tricky], temp)
            self.assertGreater(pid, 0)
            deadline = time.monotonic() + 30
            while not out.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(0.2)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), tricky)

    def test_missing_operation_is_unsupported_not_emulated(self):
        with self.assertRaisesRegex(RuntimeError, "未支持"):
            native.run("no-such-operation", [])


if __name__ == "__main__":
    unittest.main()
