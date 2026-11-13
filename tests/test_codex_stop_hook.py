import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from shiftboss.hooks import codex_stop


class CodexStopHook(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.payload = {"session_id": "boss", "turn_id": "turn-one", "cwd": "/repo",
                        "hook_event_name": "Stop"}

    def run_hook(self, result=None, error=None, payload=None):
        with (patch('shiftboss.config.control_dir', return_value=self.directory),
              patch('shiftboss.inbox.origin_git_dir', return_value=Path('/hub.git')),
              patch('shiftboss.state.read_state', return_value={"session": "boss"}),
              patch.object(codex_stop.subprocess, 'run', return_value=result, side_effect=error) as run,
              patch.object(codex_stop.sys, 'stdin', io.StringIO(json.dumps(payload or self.payload))),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(codex_stop.main(['--repo', '/repo', '--state-dir', str(self.directory)]), 0)
        return json.loads(output.getvalue()), run

    def result(self, response):
        return subprocess.CompletedProcess([], 0, json.dumps(response, ensure_ascii=False), '')

    def test_complete_brief_is_returned_on_same_turn_retry_without_ui_ack(self):
        text = '【时间】现在\n' + '完整信息中文\n' * 5000
        result = self.result({'systemMessage': text})
        first, _ = self.run_hook(result)
        self.assertEqual(first['systemMessage'], text)
        # Returning JSON is not acknowledgement that the UI displayed it.
        second, _ = self.run_hook(result)
        self.assertEqual(second['systemMessage'], text)
        updated = text + '本轮最新状态\n'
        retry, _ = self.run_hook(self.result({'systemMessage': updated}))
        self.assertEqual(retry['systemMessage'], updated)
        next_turn, _ = self.run_hook(result, payload={**self.payload, 'turn_id': 'turn-two'})
        self.assertEqual(next_turn['systemMessage'], text)
        self.assertEqual(json.loads((self.directory / 'shiftboss-stop-brief.json').read_text())['brief'], text)

    def test_block_keeps_full_snapshot_until_final_stop(self):
        response, _ = self.run_hook(self.result({'systemMessage': '完整简报', 'decision': 'block', 'reason': '处理事项'}))
        self.assertEqual(response, {'decision': 'block', 'reason': '处理事项'})
        self.assertFalse(json.loads((self.directory / 'shiftboss-stop-brief.json').read_text())['brief_returned'])
        final, _ = self.run_hook(self.result({'systemMessage': '完整简报'}))
        self.assertEqual(final['systemMessage'], '完整简报')

    def test_timeout_is_visible_and_preserves_partial_output(self):
        response, _ = self.run_hook(error=subprocess.TimeoutExpired(['brief'], 150, output=b'partial', stderr=b'error'))
        self.assertIn('超过 150 秒', response['systemMessage'])
        self.assertIn('不能据此认定无待办', response['systemMessage'])
        error = json.loads((self.directory / 'shiftboss-stop-last-error.json').read_text())
        self.assertEqual((error['stdout'], error['stderr']), ('partial', 'error'))

    def test_nonzero_child_is_visible_with_complete_raw_output(self):
        response, _ = self.run_hook(subprocess.CompletedProcess([], 2, 'stdout', 'stderr\n' * 300))
        self.assertIn('退出 2', response['systemMessage'])
        error = json.loads((self.directory / 'shiftboss-stop-last-error.json').read_text())
        self.assertEqual(error['stderr'], 'stderr\n' * 300)

    def test_empty_or_invalid_child_output_cannot_report_success(self):
        for text in ('', '{}', '{"systemMessage": ""}', 'not json'):
            with self.subTest(text=text):
                response, _ = self.run_hook(subprocess.CompletedProcess([], 0, text, ''))
                self.assertIn('简报失败', response['systemMessage'])
                self.assertEqual(json.loads((self.directory / 'shiftboss-stop-last-error.json').read_text())['stdout'], text)

    def test_foreign_session_or_copy_does_not_run_brief(self):
        for payload in ({**self.payload, 'session_id': 'worker'},
                        {**self.payload, 'cwd': '/repo.shiftboss/codex1'},
                        {**self.payload, 'hook_event_name': 'Interrupt'}):
            response, run = self.run_hook(payload=payload)
            self.assertEqual(response, {})
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
