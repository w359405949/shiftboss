"""执行方插件接口与平台脚本。"""
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest

from shiftboss import dispatch
from shiftboss import native
from shiftboss.workers import WORKERS

INTERFACE = ("PROCESS_NAMES", "CAN_QUEUE", "SESSION_WAIT", "tui_command", "configured",
             "newest_session", "step_count", "parked_at", "model_of", "queue_into",
             "sandbox_warning", "prepare")


class Workers(unittest.TestCase):
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
