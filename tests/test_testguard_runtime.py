"""已登记运行方式不新增闸；未知加载器、规则参数和 TS 测试仍受守卫约束。"""
import json
import unittest
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from shiftboss import testguard, regen


PREFIX = ['node', '--enable-source-maps', '--import', './runtime/register.mjs']
PREPARE = '(cd engine/typescript && node --input-type=module - --bootstrap < register.mts)'
RUNTIME = ['node', '--enable-source-maps', '--import', './out/typescript/bootstrap/register.mjs']
PREPARED = ['bash', '-o', 'pipefail', '-c', PREPARE + ' && exec ' + ' '.join(RUNTIME) + ' "$@"', 'node']


class RuntimeGateIdentity(unittest.TestCase):
    def find(self, old, new, prefixes=None):
        return testguard.find(lambda *args: '', 'base', None, [],
                              (json.dumps({'gates': old}), json.dumps({'gates': new})), set(),
                              runtime_prefixes=prefixes)

    def test_registered_prefix_keeps_existing_rule_identity(self):
        old = [['node', 'rules.mjs', '--case', 'same']]
        self.assertEqual(self.find(old, [[*PREFIX, 'rules.mjs', '--case', 'same']], [PREFIX]), [])

    def test_both_registered_pipeline_commands_keep_identity(self):
        plain = 'node inputs.mjs --constants | python -B inputs.py . | node inputs.mjs --stage-inputs'
        loaded = plain.replace('node ', ' '.join(PREFIX) + ' ')
        shell = ['bash', '-o', 'pipefail', '-c']
        self.assertEqual(self.find([[*shell, plain]], [[*shell, loaded]], [PREFIX]), [])

    def test_registry_required_and_other_loader_not_accepted(self):
        old = [['node', 'rules.mjs']]
        self.assertTrue(self.find(old, [[*PREFIX, 'rules.mjs']]))
        other = [*PREFIX[:-1], './other/register.mjs', 'rules.mjs']
        self.assertTrue(self.find(old, [other], [PREFIX]))

    def test_real_new_rule_or_script_still_rejected(self):
        old = [['node', 'rules.mjs', '--case', 'same']]
        self.assertTrue(self.find(old, [[*PREFIX, 'rules.mjs', '--case', 'new']], [PREFIX]))
        self.assertTrue(self.find(old, [[*PREFIX, 'new-rule.mjs', '--case', 'same']], [PREFIX]))

    def test_new_pipeline_stage_still_rejected(self):
        old = [['bash', '-c', 'node inputs.mjs --constants']]
        new = [['bash', '-c', ' '.join(PREFIX) + ' inputs.mjs --constants | node new-test.mjs']]
        self.assertTrue(self.find(old, new, [PREFIX]))

    def test_quoted_shell_or_eval_not_equated(self):
        for text in ("echo 'node inputs.mjs'", 'node inputs.mjs; echo extra',
                     'node $(command).mjs', 'node inputs.mjs || node more.mjs'):
            gate = ['bash', '-c', text.replace('node ', ' '.join(PREFIX) + ' ')]
            self.assertEqual(testguard.command_identity(gate, [PREFIX]), tuple(gate))
        self.assertTrue(self.find([['node', 'rules.mjs']], [[*PREFIX, '--eval', 'new_test()']], [PREFIX]))

    def test_bad_prefix_declaration_fails_closed(self):
        for value in (['node'], ['node', None], 'node --anything'):
            with self.assertRaises(ValueError):
                testguard.command_identity(['node', 'rules.mjs'], [value])

    def test_typescript_test_framework_import_not_hidden_by_suffix(self):
        for suffix in ('.ts', '.mts', '.cts'):
            path = 'new-check' + suffix
            diff = "+++ b/" + path + "\n+import assert from 'node:assert/strict';\n"
            found = testguard.find(lambda *args: diff, 'base', None, [], (None, None), set())
            self.assertIn('测试目录外的测试框架导入 ' + path, found)

    def test_prepared_runtime_keeps_original_consumer_and_all_arguments(self):
        args = ['rules.mjs', '--case', 'same', 'space and "quotes"', 'line\nbreak']
        self.assertEqual(testguard.command_identity([*PREPARED, *args], [PREPARED]), tuple(['node', *args]))
        self.assertEqual(self.find([[*PREFIX, 'rules.mjs']], [[*PREPARED, 'rules.mjs']], [PREFIX, PREPARED]), [])

    def test_unregistered_or_altered_preparation_is_not_existing_rule(self):
        self.assertTrue(self.find([['node', 'rules.mjs']], [[*PREPARED, 'rules.mjs']]))
        changed = PREPARED.copy()
        changed[4] = changed[4].replace('register.mts', 'unknown.mts')
        self.assertTrue(self.find([['node', 'rules.mjs']], [[*changed, 'rules.mjs']], [PREPARED]))
        self.assertTrue(self.find([['node', 'rules.mjs']], [[*PREPARED, 'other.mjs']], [PREPARED]))
        self.assertTrue(self.find([['node', 'rules.mjs']], [[*PREPARED, 'rules.mjs', '--new-rule']], [PREPARED]))

    def test_prepare_once_then_existing_pipeline_keeps_each_leg(self):
        plain = 'node inputs.mjs --constants | python -B inputs.py . | node inputs.mjs --stage-inputs'
        loaded = PREPARE + ' && ' + plain.replace('node ', ' '.join(RUNTIME) + ' ')
        shell = ['bash', '-o', 'pipefail', '-c']
        self.assertEqual(self.find([[*shell, plain]], [[*shell, loaded]], [PREPARED]), [])
        for altered in [loaded + ' | node extra.mjs',
                        loaded.replace('inputs.py .', 'inputs.py --new'),
                        loaded.replace('./out/typescript/bootstrap/register.mjs', './other.mjs'),
                        PREPARE + ' && ' + loaded,
                        loaded + '; echo extra']:
            self.assertTrue(self.find([[*shell, plain]], [[*shell, altered]], [PREPARED, PREPARED]))


class PreparedGenerationStage(unittest.TestCase):
    def stage(self, command, name='fixture'):
        return {'name': name, 'command': command, 'inputs': ['source/']}

    def validate(self, stages, prefixes=None):
        with patch('shiftboss.config.load', return_value={'verify': {'runtime_prefixes': prefixes or []}}):
            return regen.validate_stages({'stages': stages})

    def test_only_registered_preparation_wraps_one_original_node_stage(self):
        stage = self.stage([*PREPARED, 'engine/generate.mjs', '--value', 'same'])
        self.assertEqual(self.validate([stage], [PREPARED]), [stage])
        with self.assertRaises(ValueError):
            self.validate([stage])
        for args in [['--eval', 'anything'], ['engine/generate.mjs', 'x;second'],
                     ['engine/generate.mjs', '--print'], ['--import', 'other.mjs', 'engine/generate.mjs']]:
            with self.assertRaises(ValueError):
                self.validate([self.stage([*PREPARED, *args])], [PREPARED])

    def test_pipeline_and_unknown_shell_remain_invalid_generation_stages(self):
        for script in [PREPARE + ' && node a.mjs | node b.mjs', 'node a.mjs && node b.mjs']:
            with self.assertRaises(ValueError):
                self.validate([self.stage(['bash', '-o', 'pipefail', '-c', script])], [PREPARED])

    def test_duplicate_logical_consumer_not_hidden_by_runtime_preparation(self):
        with self.assertRaisesRegex(ValueError, '重复'):
            self.validate([self.stage(['node', 'engine/generate.mjs']),
                           self.stage([*PREPARED, 'engine/generate.mjs'], 'second')], [PREPARED])

    def test_wrapped_node_still_has_independent_output_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def producer(command, env):
                self.assertEqual(command, [*PREPARED, 'engine/generate.mjs'])
                (root / 'undeclared.json').write_text('new')
                Path(env[regen.REPORT_ENV]).write_text(json.dumps({
                    'schema': regen.SCHEMA, 'files': ['data/value.json'],
                    'written': ['data/value.json'], 'retained': []}))
                return subprocess.CompletedProcess(command, 0, '', '')
            with (patch.object(regen, 'REPO', root),
                  patch('shiftboss.config.load', return_value={'verify': {'runtime_prefixes': [PREPARED]}}),
                  patch.object(regen, 'run_producer', side_effect=producer),
                  patch.object(regen, 'changed_files', side_effect=[{}, {'undeclared.json': 'new'}]) as observed,
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertFalse(regen.run_all('fixture', [[*PREPARED, 'engine/generate.mjs']], outputs=[]))
                self.assertEqual(observed.call_count, 2)
            self.assertIn('未声明', next((root/'out/regen-failures').glob('*.log')).read_text())


if __name__ == '__main__':
    unittest.main()
