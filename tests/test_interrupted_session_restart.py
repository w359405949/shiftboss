from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import dispatch, inbox, recover, state


class InterruptedSessionRestartTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='shiftboss-interrupted-session-')
        self.addCleanup(self.directory.cleanup)
        self.copy = Path(self.directory.name)
        self.source = self.copy / 'candidate.ts'
        self.source.write_text('unsubmitted original source')
        self.entry = {'session_id': 'original-session', 'copy': str(self.copy),
                      'round': 20, 'dispatched_at': 'original-dispatch'}

    def invoke(self, state, entry=None, dry_run=False):
        with patch.object(inbox, 'session_state', return_value=state), \
                patch.object(dispatch, 'close_session') as close:
            dispatch.restart_interrupted_session('task', str(self.copy),
                                                 self.entry if entry is None else entry,
                                                 Path('/hub.git'), dry_run=dry_run)
            return close.call_args_list

    def test_failed_terminal_closes_only_its_copy_and_preserves_candidates_and_identity(self):
        original = dict(self.entry)
        calls = self.invoke('执行中断')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args, (str(self.copy),))
        self.assertEqual(self.entry, original)
        self.assertEqual(self.source.read_text(), 'unsubmitted original source')

    def test_healthy_or_parked_session_cannot_be_terminated(self):
        for state in ('在干活', '停着', '做完了没推', '?'):
            with self.subTest(state=state), \
                    patch.object(inbox, 'session_state', return_value=state), \
                    patch.object(dispatch, 'close_session') as close:
                with self.assertRaises(ValueError):
                    dispatch.restart_interrupted_session('task', str(self.copy), self.entry,
                                                         Path('/hub.git'))
                close.assert_not_called()

    def test_user_closed_session_cannot_be_restarted(self):
        with patch.object(inbox, 'session_state', return_value='执行中断'), \
                patch.object(dispatch, 'close_session') as close:
            with self.assertRaises(ValueError):
                dispatch.restart_interrupted_session('task', str(self.copy),
                                                     {**self.entry, 'closed': True},
                                                     Path('/hub.git'))
            close.assert_not_called()

    def test_foreign_copy_cannot_be_terminated(self):
        with patch.object(inbox, 'session_state', return_value='执行中断'), \
                patch.object(dispatch, 'close_session') as close:
            with self.assertRaises(ValueError):
                dispatch.restart_interrupted_session('task', str(self.copy),
                                                     {**self.entry, 'copy': '/another-copy'},
                                                     Path('/hub.git'))
            close.assert_not_called()

    def test_dry_run_checks_failure_without_closing_terminal(self):
        self.assertEqual(self.invoke('执行中断', dry_run=True), [])

    def test_restart_acquires_shared_recovery_lock_before_workspace(self):
        events = []

        @contextmanager
        def locked(repo):
            events.append('recovery')
            yield True

        def workspace(*args):
            events.append('workspace')
            return False

        with patch.object(recover, 'recovery_lock', side_effect=locked), \
                patch.object(state, 'take_workspace', side_effect=workspace), \
                patch.object(dispatch, '_main', return_value=0):
            result = dispatch.main(['task', '--recover', '--restart-interrupted'])
        self.assertEqual(result, 0)
        self.assertEqual(events, ['recovery', 'workspace'])

    def test_existing_recovery_prevents_second_restart(self):
        @contextmanager
        def busy(repo):
            yield False

        with patch.object(recover, 'recovery_lock', side_effect=busy), \
                patch.object(state, 'take_workspace') as workspace, \
                patch.object(dispatch, '_main') as invoke:
            result = dispatch.main(['task', '--recover', '--restart-interrupted'])
        self.assertEqual(result, 2)
        workspace.assert_not_called()
        invoke.assert_not_called()


if __name__ == '__main__':
    unittest.main()
