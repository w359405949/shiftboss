import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss.workers import codex


class CodexQueueResultTests(unittest.TestCase):
    def invoke(self, result, consume=False):
        with tempfile.TemporaryDirectory(prefix='shiftboss-queue-receipt-') as directory:
            root = Path(directory)
            rollout = root / 'rollout.jsonl'
            rollout.write_text('')

            def run(*args, **kwargs):
                if isinstance(result, Exception):
                    raise result
                if consume:
                    rollout.write_text(json.dumps({'payload': {
                        'type': 'message', 'role': 'user', 'content': 'continue-task'}}) + '\n')
                return result

            output = io.StringIO()
            with patch.object(codex, 'rollout_of', return_value=rollout), \
                    patch.object(codex, 'resolve_codex', return_value=Path('/codex')), \
                    patch.object(codex, 'worker_home', return_value=root, create=True), \
                    patch.object(codex.subprocess, 'run', side_effect=run), \
                    patch.object(codex.time, 'sleep') as sleep, \
                    contextlib.redirect_stdout(output):
                accepted = codex.queue_into('thread', 'continue-task', lambda: True)
            return accepted, output.getvalue(), sleep.call_count

    def test_failed_command_keeps_complete_output_and_does_not_wait_for_ack(self):
        result = subprocess.CompletedProcess([], 2, 'output one\noutput two\n',
                                             'error one\nerror two\n')
        accepted, output, sleeps = self.invoke(result)
        self.assertFalse(accepted)
        self.assertEqual(sleeps, 0)
        self.assertIn('退出 2', output)
        self.assertIn(result.stdout, output)
        self.assertIn(result.stderr, output)

    def test_timeout_keeps_partial_bytes_and_remains_unconfirmed(self):
        result = subprocess.TimeoutExpired('codex queue', 30,
                                          output=b'partial output\n', stderr=b'partial error\n')
        accepted, output, sleeps = self.invoke(result)
        self.assertFalse(accepted)
        self.assertEqual(sleeps, 0)
        self.assertIn('投递未确认', output)
        self.assertIn('partial output\n', output)
        self.assertIn('partial error\n', output)

    def test_consumed_successful_command_still_confirms_delivery(self):
        accepted, output, sleeps = self.invoke(subprocess.CompletedProcess([], 0, '', ''),
                                               consume=True)
        self.assertTrue(accepted)
        self.assertEqual(sleeps, 1)
        self.assertEqual(output, '')


if __name__ == '__main__':
    unittest.main()
