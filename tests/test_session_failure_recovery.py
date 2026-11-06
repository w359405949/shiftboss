import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from unittest.mock import patch

from shiftboss import dispatch, inbox, recover, config
from shiftboss.workers import codex

START = '2026-11-05T08:46:00Z'
FAILED = '2026-11-05T08:51:20Z'
RESUMED = '2026-11-05T13:39:25Z'


def event(kind, stamp=FAILED, turn='one', **extra):
    return {'type': 'event_msg', 'timestamp': stamp,
            'payload': {'type': kind, 'turn_id': turn, **extra}}


class SessionFailureRecovery(unittest.TestCase):
    def setUp(self):
        for mock in (patch.object(recover, 'recovery_lock', return_value=nullcontext(True)),
                     patch('shiftboss.state.require_dispatcher')):
            mock.start()
            self.addCleanup(mock.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rollout = Path(self.temp.name) / 'rollout.jsonl'
        self.lookup = patch.object(codex, 'rollout_of', return_value=self.rollout)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)
        self.entry = {'session_id': 'session-one', 'executor': 'codex', 'copy': '/repo/codex1',
                      'dispatched_at': '2026-11-05T16:45:00+08:00'}
        self.failure = [event('task_started', START), event('task_complete',
                        error={'codex_error_info': 'server_overloaded', 'message': 'Selected model is at capacity.'}),
                        event('token_count'), event('item_completed')]

    def write(self, rows, tail=''):
        self.rollout.write_text(''.join(json.dumps(x) + '\n' for x in rows) + tail)

    def state(self):
        with patch.object(inbox, 'last_push', return_value=None), patch.object(dispatch, 'copy_processes', return_value=[object()]):
            return inbox.session_state('one', None, self.entry, Path('/hub.git'))

    def test_failure_survives_bookkeeping_and_live_terminal(self):
        self.write(self.failure)
        self.assertEqual(codex.failed_at('session-one'), FAILED)
        self.assertEqual(codex.parked_at('session-one'), FAILED)
        self.assertEqual(self.state(), '执行中断')
        self.assertTrue(recover.still_running(self.state(), '自走'))

    def test_new_turn_clears_old_failure_and_late_old_terminal_is_ignored(self):
        self.write(self.failure + [event('task_started', RESUMED, turn='two'),
                                  event('task_complete', RESUMED, error={'message': 'late'}, turn='one'),
                                  event('item_completed', RESUMED)])
        self.assertIsNone(codex.failed_at('session-one'))
        self.assertIsNone(codex.parked_at('session-one'))
        self.assertEqual(self.state(), '在干活')

    def test_normal_completion_after_bookkeeping_is_not_resumed(self):
        self.write([event('task_started', START), event('task_complete', error=None), event('token_count'), event('item_completed')])
        self.assertEqual(codex.parked_at('session-one'), FAILED)
        self.assertIsNone(codex.failed_at('session-one'))
        self.assertEqual(self.state(), '做完了没推')
        self.assertFalse(recover.still_running(self.state(), '自走'))

    def test_user_abort_is_not_an_error_to_auto_resume(self):
        self.write(self.failure + [event('turn_aborted', RESUMED)])
        self.assertIsNone(codex.failed_at('session-one'))
        self.assertEqual(codex.parked_at('session-one'), RESUMED)
        self.assertFalse(recover.still_running(self.state(), '自走'))

    def test_quoted_error_and_failed_tool_are_not_terminal_failure(self):
        self.write([event('task_started', START), {'type': 'response_item', 'timestamp': FAILED,
                    'payload': {'type': 'message', 'role': 'user', 'content': 'task_complete server_overloaded'}},
                    {'type': 'response_item', 'timestamp': FAILED,
                     'payload': {'type': 'custom_tool_call_output', 'output': 'ERROR model at capacity'}}, event('token_count')])
        self.assertIsNone(codex.failed_at('session-one'))
        self.assertEqual(self.state(), '在干活')

    def test_incomplete_json_is_unknown_and_cannot_trigger_recovery(self):
        self.write(self.failure, tail='{"type": "event_msg", "payload":')
        self.assertIsNone(codex.failed_at('session-one'))
        self.assertIsNone(codex.parked_at('session-one'))
        self.assertEqual(self.state(), '在干活')

    def test_old_failure_before_dispatch_does_not_interrupt_new_round(self):
        self.write(self.failure)
        self.entry['dispatched_at'] = '2026-11-05T22:00:00+08:00'
        self.assertEqual(self.state(), '在干活')

    def test_closed_or_unsupported_worker_failure_stays_unknown(self):
        self.write(self.failure)
        self.assertIsNone(inbox.session_failed_at({**self.entry, 'closed': True}))
        with patch.dict('shiftboss.workers.WORKERS', {'codex': object()}):
            self.assertIsNone(inbox.session_failed_at(self.entry))

    def test_running_lines_includes_error_but_never_finished_delivery_or_closed_line(self):
        self.write(self.failure)
        for closed, ready, expected in [(False, False, [('one', '执行中断')]), (True, False, []), (False, True, [])]:
            rows = [{'name': 'one', 'state': '可核对' if ready else '进行中'}]
            with (patch.object(dispatch, 'load_sessions', return_value={'one': {**self.entry, 'closed': closed}}),
                  patch.object(inbox, 'survey', return_value=rows), patch.object(inbox, 'origin_git_dir', return_value=Path('/hub.git')),
                  patch.object(inbox, 'taskbook_of', return_value=({'交付模式': '自走'}, '')),
                  patch.object(inbox, 'last_push', return_value=None), patch.object(dispatch, 'copy_processes', return_value=[object()])):
                self.assertEqual(recover.running_lines(Path('/repo')), expected)

    def test_check_mode_does_not_mutate_or_start_process(self):
        with (patch.object(recover, 'restart_resident'), patch.object(recover, 'running_lines', return_value=[('one', '执行中断')]),
              patch.object(recover.subprocess, 'run') as run, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(recover.main(['--check']), 0)
            self.assertIn('执行中断', output.getvalue())
            run.assert_not_called()

    def test_recovery_attempt_preserves_resume_command_and_reports_launch_failure(self):
        for code in (0, 2):
            with (patch.object(recover, 'restart_resident'), patch.object(recover, 'running_lines', return_value=[('one', '执行中断')]),
                  patch.object(config, 'automation', return_value=True),
                  patch.object(recover.subprocess, 'run', return_value=subprocess.CompletedProcess([], code)) as run,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(recover.main([]), int(code != 0))
                self.assertEqual(run.call_args.args[0][-2:], ['one', '--recover'])
                self.assertNotIn('--fresh', run.call_args.args[0])
                self.assertEqual('恢复失败' in errors.getvalue(), code != 0)
