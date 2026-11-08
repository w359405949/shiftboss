import io
import hashlib
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import config, inbox, state
from shiftboss.hooks import context_refresh


class ContextRefreshHooks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = self.directory / 'project'
        self.repo.mkdir()
        self.control = self.directory / 'control'
        self.control.mkdir()
        state.write_state(self.control / 'dispatcher.json', {'session': 'owner'})
        (self.repo / 'AGENTS.md').write_text('Read README.md before changing anything.\n')
        (self.repo / 'README.md').write_text('Current project boundary: preserve user data.\n')
        (self.repo / 'shiftboss').mkdir()
        (self.repo / 'shiftboss/README.md').write_text(
            '### 2.0 节奏\nExisting finite results are reused.\n'
            '### 2.1 甲方\nOnly dispatchers merge.\n'
            '### 4.3 写权\nDisposable outputs may be deleted.\n'
            '### 5.1 判据\nMachine-checkable rules use scripts.\n')
        self.payload = {'hook_event_name': 'SessionStart', 'session_id': 'owner',
                        'cwd': str(self.repo), 'source': 'compact'}
        for mock in (patch.object(config, 'control_dir', return_value=self.control),
                     patch.object(inbox, 'origin_git_dir', return_value=self.directory / 'hub.git')):
            mock.start()
            self.addCleanup(mock.stop)

    def context(self, payload=None):
        return context_refresh.refresh(self.repo, payload or self.payload)['hookSpecificOutput']['additionalContext']

    def test_compact_and_resume_refresh_actual_documents_instead_of_saved_summary(self):
        first = self.context()
        self.assertIn('Only dispatchers merge.', first)
        self.assertIn('Machine-checkable rules use scripts.', first)
        self.assertIn('Disposable outputs may be deleted.', first)
        (self.repo / 'README.md').write_text('New user constraint: transcription and fixes are separate.\n')
        second = self.context({**self.payload, 'source': 'resume'})
        self.assertNotIn('preserve user data.', second)
        self.assertIn('transcription and fixes are separate.', second)
        report = state.read_state(self.control / 'context-refresh.json')
        self.assertEqual(report['source'], 'resume')
        self.assertEqual(report['status'], 'emitted')
        self.assertEqual(report['contextBytes'], len(second.encode()))
        self.assertNotIn('additionalContext', report)

    def test_worker_foreign_directory_and_unrelated_events_never_read_private_context(self):
        cases = [{'session_id': 'worker'}, {'cwd': str(self.repo / 'worker')},
                 {'hook_event_name': 'PostCompact'}, {'hook_event_name': 'Interrupt'},
                 {'source': 'startup'}, {'source': 'clear'}, {'source': None}]
        for changes in cases:
            with self.subTest(changes=changes), patch.object(context_refresh, 'build') as build:
                self.assertIsNone(context_refresh.refresh(self.repo, {**self.payload, **changes}))
                build.assert_not_called()

    def test_current_taskbook_and_live_stage_are_inputs_but_not_execution_claims(self):
        plan = self.repo / 'shiftboss/plans/example-task'
        plan.mkdir(parents=True)
        (plan / 'meta.json').write_text(json.dumps({'轮次': 3, '你的工作副本': '/worker-a', '快速检查': []}))
        state.write_state(self.control / 'sessions.json', {
            'example-task': {'session_id': 'a'}, 'already-closed': {'session_id': 'b', 'closed': True}})
        (self.control / 'verification').mkdir()
        state.write_state(self.control / 'verification/index.json', {'active': {'commit': 'new-source', 'stage': 'gate-3'}})
        text = self.context()
        self.assertIn('example-task：第3轮', text)
        self.assertNotIn('already-closed', text)
        self.assertIn('gate-3', text)
        self.assertIn('台账未关闭不等于实际在跑', text)
        self.assertIn('inbox list', text)
        state.write_state(self.control / 'verification/index.json', {'active': {'commit': 'later', 'stage': 'generate'}})
        self.assertIn('later', self.context())

    def test_phase_is_prioritized_and_reread_after_compact_and_resume(self):
        phase = self.control / 'PHASE.md'
        phase.write_text('# TS 转写推进\n目标：成批交付，不逐 helper 造样本。\n')
        first = self.context()
        self.assertIn(phase.read_text().strip(), first)
        self.assertLess(first.index('[当前阶段目标：'), first.index('[台账未关闭'))
        phase.write_text('# 下一阶段\n目标：核实际发布。\n')
        second = self.context({**self.payload, 'source': 'resume'})
        self.assertNotIn('不逐 helper 造样本', second)
        self.assertIn('核实际发布', second)
        record = state.read_state(self.control / 'context-refresh.json')['phase']
        self.assertEqual(record['sha256'], hashlib.sha256(phase.read_bytes()).hexdigest())
        self.assertTrue(record['fullyEmitted'])

    def test_cleared_or_deleted_phase_does_not_reuse_a_previous_goal(self):
        phase = self.control / 'PHASE.md'
        phase.write_text('# Old strategy\n')
        self.assertIn('Old strategy', self.context())
        phase.write_text('')
        self.assertNotIn('Old strategy', self.context())
        self.assertEqual(state.read_state(self.control / 'context-refresh.json')['phase']['status'], 'cleared')
        phase.unlink()
        self.assertNotIn('Old strategy', self.context())
        self.assertEqual(state.read_state(self.control / 'context-refresh.json')['phase']['status'], 'unset')

    def test_large_phase_keeps_an_explicit_full_read_requirement_not_a_partial_strategy(self):
        phase = self.control / 'PHASE.md'
        phase.write_text('phase-complete-rule-' * 2000)
        text = self.context()
        self.assertNotIn('phase-complete-rule-', text)
        self.assertIn(str(phase), text)
        self.assertIn('先读取全文', text)
        self.assertLessEqual(len(text.encode()), context_refresh.MAX_BYTES)
        record = state.read_state(self.control / 'context-refresh.json')
        self.assertFalse(record['phase']['fullyEmitted'])
        self.assertIn('当前阶段目标：' + str(phase), record['omitted'])

    def test_phase_read_failure_is_explicit(self):
        (self.control / 'PHASE.md').write_bytes(b'\xff')
        text = self.context()
        self.assertIn('阶段目标', text)
        self.assertIn('读取失败', text)
        record = state.read_state(self.control / 'context-refresh.json')
        self.assertEqual(record['phase']['status'], 'error')
        self.assertTrue(any('PHASE.md' in error for error in record['errors']))

    def test_large_documents_are_explicitly_omitted_not_half_rules(self):
        sentinel = 'important-complete-rule-'
        (self.repo / 'README.md').write_text(sentinel * 3000)
        text = self.context()
        self.assertLessEqual(len(text.encode()), context_refresh.MAX_BYTES)
        self.assertNotIn(sentinel, text)
        self.assertIn('相关操作前重新读取', text)
        self.assertIn('README.md', state.read_state(self.control / 'context-refresh.json')['omitted'])
        self.assertIn('Only dispatchers merge.', text)

    def test_missing_section_and_oversized_file_are_visible(self):
        (self.repo / 'shiftboss/README.md').write_text('No longer has those headings.\n')
        (self.repo / 'README.md').write_bytes(b'x' * (context_refresh.DOCUMENT_BYTES + 1))
        text = self.context()
        self.assertIn('缺第2.1节', text)
        self.assertIn('超过文档读取上限', text)
        self.assertEqual(len(state.read_state(self.control / 'context-refresh.json')['errors']), 5)

    def test_invalid_private_state_is_not_silently_treated_as_empty(self):
        (self.control / 'todo.json').write_text('{broken')
        with self.assertRaises(SystemExit):
            self.context()

    def test_installer_is_idempotent_preserves_existing_handlers_and_trust(self):
        path = self.directory / 'hooks.json'
        old = {'description': 'keep', 'hooks': {
            'Stop': [{'hooks': [{'type': 'command', 'command': 'brief'}]}],
            'SessionStart': [{'hooks': [{'type': 'command', 'command': 'recover'}]}]}}
        path.write_text(json.dumps(old))
        trust = self.directory / 'hook-trust.json'
        trust.write_text('untouched')
        self.assertTrue(context_refresh.install(self.repo, path))
        first = path.read_bytes()
        self.assertFalse(context_refresh.install(self.repo, path))
        self.assertEqual(first, path.read_bytes())
        installed = json.loads(first)
        self.assertEqual(installed['hooks']['Stop'], old['hooks']['Stop'])
        self.assertEqual(installed['hooks']['SessionStart'][0], old['hooks']['SessionStart'][0])
        self.assertEqual(installed['hooks']['SessionStart'][1]['matcher'], 'compact|resume')
        self.assertNotIn('PostCompact', installed['hooks'])
        self.assertEqual(trust.read_text(), 'untouched')

    def test_cli_outputs_supported_sessionstart_json_and_bad_input_has_no_side_effect(self):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(self.payload))), redirect_stdout(output):
            self.assertEqual(context_refresh.main(['--repo', str(self.repo)]), 0)
        message = json.loads(output.getvalue())['hookSpecificOutput']
        self.assertEqual(message['hookEventName'], 'SessionStart')
        self.assertIn('Current project boundary', message['additionalContext'])
        for value in ('[]', 'null'):
            with patch('sys.stdin', io.StringIO(value)), patch.object(context_refresh, 'refresh') as refresh:
                self.assertEqual(context_refresh.main(['--repo', str(self.repo)]), 0)
                refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
