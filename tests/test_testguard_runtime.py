"""已登记运行方式不新增闸；未知加载器、规则参数和 TS 测试仍受守卫约束。"""
import json
import unittest

from shiftboss import testguard


PREFIX = ['node', '--enable-source-maps', '--import', './runtime/register.mjs']


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


if __name__ == '__main__':
    unittest.main()
