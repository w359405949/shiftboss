import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import config, inbox, native, recover, state
from shiftboss.hooks import auto_recover


class AutoRecoveryHooks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = self.directory / 'project'
        self.repo.mkdir()
        self.control = self.directory / 'control'
        self.control.mkdir()
        state.write_state(self.control / 'dispatcher.json', {'session': 'owner'})
        for mock in (patch.object(config, 'control_dir', return_value=self.control),
                     patch.object(inbox, 'origin_git_dir', return_value=self.directory / 'hub.git'),
                     patch.object(config, 'automation', return_value=True),
                     patch.object(recover, 'REPO', self.repo)):
            mock.start()
            self.addCleanup(mock.stop)
        self.payload = {'hook_event_name': 'SessionStart', 'session_id': 'owner',
                        'cwd': str(self.repo.parent), 'source': 'resume'}

    def test_authority_scope_and_automation_switch(self):
        for changes in ({'session_id': 'worker'}, {'cwd': str(self.repo / 'worker')},
                        {'hook_event_name': 'SessionEnd'}, {'session_id': None}, {'cwd': None}):
            with patch.object(native, 'spawn') as spawn:
                auto_recover.launch(self.repo, {**self.payload, **changes})
                spawn.assert_not_called()
        with patch.object(config, 'automation', return_value=False), patch.object(native, 'spawn') as spawn:
            auto_recover.launch(self.repo, self.payload)
            spawn.assert_not_called()

    def test_all_events_launch_detached_original_recovery_not_root_resume(self):
        for event in auto_recover.EVENTS:
            with patch.object(native, 'spawn', return_value=42) as spawn:
                self.assertEqual(auto_recover.launch(self.repo, {**self.payload, 'hook_event_name': event}), 0)
                argv, cwd = spawn.call_args.args
                self.assertEqual(argv[3], 'shiftboss.hooks.auto_recover')
                self.assertIn('--run', argv)
                self.assertNotIn('dispatch', argv)
                self.assertNotIn('--fresh', argv)
                self.assertEqual(cwd, str(self.repo))

    def test_event_performs_resume_and_preserves_dirty_file(self):
        dirty = self.repo / 'unfinished.ts'
        dirty.write_bytes(b'const incomplete = 1;\n')
        with (patch.object(recover, 'restart_resident'),
              patch.object(recover, 'running_lines', return_value=[('broken-task', '执行中断')]),
              patch.object(recover.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run):
            self.assertEqual(auto_recover.perform(self.repo, self.payload), 0)
            argv = run.call_args.args[0]
            self.assertEqual(argv[-2:], ['broken-task', '--recover'])
            self.assertNotIn('--fresh', argv)
            self.assertEqual(dirty.read_bytes(), b'const incomplete = 1;\n')
        report = state.read_state(self.control / 'auto-recover.json')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['returncode'], 0)
        self.assertIn('broken-task', (self.control / 'auto-recover.log').read_text())

    def test_rapid_events_do_not_repeat_recovery_and_later_event_can_retry_failure(self):
        with patch.object(recover, 'run', return_value=2) as run:
            self.assertEqual(auto_recover.perform(self.repo, self.payload), 2)
            self.assertEqual(auto_recover.perform(self.repo, self.payload), 0)
            run.assert_called_once()
            self.assertEqual(state.read_state(self.control / 'auto-recover.json')['returncode'], 2)
            record = state.read_state(self.control / 'auto-recover.json')
            record['started_epoch'] -= 61
            state.write_state(self.control / 'auto-recover.json', record)
            auto_recover.perform(self.repo, self.payload)
            self.assertEqual(run.call_count, 2)

    def test_os_lock_serializes_events_and_manual_entry_and_releases_on_exception(self):
        with recover.recovery_lock(self.repo) as acquired:
            self.assertTrue(acquired)
            with recover.recovery_lock(self.repo) as second:
                self.assertFalse(second)
            with patch.object(recover, 'run') as run, patch.object(state, 'require_dispatcher'):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(recover.main([]), 0)
                auto_recover.perform(self.repo, self.payload)
                run.assert_not_called()
        with self.assertRaises(RuntimeError):
            with recover.recovery_lock(self.repo) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError('interrupted')
        with recover.recovery_lock(self.repo) as acquired:
            self.assertTrue(acquired)

    def test_changed_dispatcher_and_disabled_switch_rechecked_in_background(self):
        state.write_state(self.control / 'dispatcher.json', {'session': 'another-owner'})
        with patch.object(recover, 'run') as run:
            auto_recover.perform(self.repo, self.payload)
            run.assert_not_called()
        state.write_state(self.control / 'dispatcher.json', {'session': 'owner'})
        with patch.object(config, 'automation', return_value=False), patch.object(recover, 'run') as run:
            auto_recover.perform(self.repo, self.payload)
            run.assert_not_called()

    def test_launch_exception_is_persisted_without_success_claim(self):
        with patch.object(recover, 'run', side_effect=RuntimeError('account unavailable')):
            self.assertEqual(auto_recover.perform(self.repo, self.payload), 1)
        self.assertEqual(state.read_state(self.control / 'auto-recover.json')['status'], 'failed')
        self.assertIn('account unavailable', (self.control / 'auto-recover.log').read_text())
        from shiftboss import brief
        self.assertIn('退出 1', brief.recovery_lines(self.directory / 'hub.git')[0])

    def test_prompt_claim_finishes_before_recovery_and_bad_input_is_ignored(self):
        order = []
        prompt = {**self.payload, 'hook_event_name': 'UserPromptSubmit', 'prompt': 'continue'}
        with (patch('sys.stdin', io.StringIO(json.dumps(prompt))),
              patch.object(auto_recover.subprocess, 'run', side_effect=lambda *a, **k: (order.append('claim') or subprocess.CompletedProcess([], 0))),
              patch.object(auto_recover, 'launch', side_effect=lambda *a: (order.append('launch') or 0))):
            self.assertEqual(auto_recover.main(['--repo', str(self.repo)]), 0)
        self.assertEqual(order, ['claim', 'launch'])
        for value in ('{broken', '[]', 'null'):
            with patch('sys.stdin', io.StringIO(value)), patch.object(auto_recover, 'launch') as launch:
                self.assertEqual(auto_recover.main(['--repo', str(self.repo)]), 0)
                launch.assert_not_called()

    def test_installer_preserves_other_hooks_and_is_idempotent(self):
        path = self.directory / 'hooks.json'
        stop = [{'hooks': [{'type': 'command', 'command': 'custom-stop'}]}]
        path.write_text(json.dumps({'description': 'keep', 'hooks': {'Stop': stop,
             'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'python -m shiftboss.hooks.claim_dispatcher --repo x'}]}]}}))
        self.assertTrue(auto_recover.install(self.repo, path))
        saved = path.read_bytes()
        self.assertFalse(auto_recover.install(self.repo, path))
        self.assertEqual(path.read_bytes(), saved)
        installed = json.loads(saved)
        self.assertEqual(installed['description'], 'keep')
        self.assertEqual(installed['hooks']['Stop'], stop)
        self.assertEqual(len(installed['hooks']['UserPromptSubmit']), 1)
        self.assertEqual(installed['hooks']['Interrupt'][0]['hooks'][0]['timeout'], 3)

    def test_residents_skip_intentional_stop_and_restore_only_crashed_periodic_lane(self):
        from shiftboss import patrol, verify
        verify_dir, patrol_dir = self.directory / 'verify', self.directory / 'patrol'
        verify_dir.mkdir()
        patrol_dir.mkdir()
        for directory, lock in ((verify_dir, 'worker.lock'), (patrol_dir, 'worker.lock'), (patrol_dir, 'periodic.lock')):
            (directory / lock).write_text('99999999')
        (verify_dir / 'stop').touch()
        (patrol_dir / 'stop').touch()
        with (patch.object(verify, 'origin', return_value=self.directory / 'hub.git'),
              patch.object(verify, 'state_dir', return_value=verify_dir),
              patch.object(patrol, 'state_dir', return_value=patrol_dir),
              patch.object(config, 'copies_dir', return_value=self.directory),
              patch.object(native, 'spawn', return_value=42) as spawn,
              redirect_stdout(io.StringIO())):
            recover.restart_resident(False)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[0][3:5], ['shiftboss.patrol', 'periodic'])
            self.assertNotIn('start', spawn.call_args.args[0])
