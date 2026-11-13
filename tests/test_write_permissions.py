"""业务跨目录开放，检查授权在真实 Git 推送/合并入口仍然有效。"""

import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from shiftboss import dispatch, taskbook
from shiftboss.permissions import check_hits
from shiftboss.outputs import CATALOG, catalog_document


class WritePermissionAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        shutil.copy2(Path(__file__).parent / 'fixtures/shiftboss.toml', self.repo / 'shiftboss.toml')
        with (self.repo / 'shiftboss.toml').open('a') as file:
            file.write('\n[guard]\nprotected = ["engine/tools/", "engine/frozen.ts"]\n'
                       'layout_paths = ["layout/"]\n'
                       '\n[verify]\ncheck_paths = ["scripts/check.mjs"]\n')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Write permissions')
        self.git('config', 'user.email', 'permissions@localhost')
        self.book = self.repo / 'shiftboss/plans/one'
        self.book.mkdir(parents=True)
        self.meta = {'分支': 'codex/one', '改检查': '否', '写入范围': ['engine/one/'],
                     '快速检查': [], '子类型': 'task', '完成标准': '成形', '破坏性': '否'}
        self.save_meta()
        (self.book / 'contract.md').write_text('# one\n')
        (self.book / 'rounds.md').write_text('# rounds\n')
        (self.book / 'delivery.md').write_text('执行状态：完成\n')
        self.write('engine/other/value.ts', 'export const value = 1;\n')
        self.write('engine/tools/generator.ts', 'export const value = 1;\n')
        self.write('engine/frozen.ts', 'export const value = 1;\n')
        self.write('scripts/check.mts', 'export const value = 1;\n')
        self.write('layout/screen.json', '{}\n')
        commands = [[sys.executable, '-c', 'pass']]
        self.write('regen.json', json.dumps({'steps': commands, 'gates': commands}))
        self.write('one.json', '{}\n')
        self.write(CATALOG, json.dumps(catalog_document([{'command': commands[0], 'files': ['one.json']}])))
        self.commit('baseline')
        remote = Path(self.temp.name) / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', '-q', 'origin', 'main')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], text=True,
                              capture_output=True, check=True).stdout.strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def save_meta(self):
        (self.book / 'meta.json').write_text(json.dumps(self.meta))

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)

    def incoming(self, paths, *, allowed=False, self_grant=False):
        if allowed:
            self.meta['改检查'] = '是'
            self.save_meta()
            self.commit('authorize checks on main')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-c', 'codex/one')
        if self_grant:
            self.meta['改检查'] = '是'
            self.save_meta()
        for path in paths:
            self.write(path, '{}\n' if path.endswith('.json') else 'export const value = 2;\n')
        # Existing layout baseline must actually change.
        if 'layout/screen.json' in paths:
            self.write('layout/screen.json', '{"width": 10}\n')
        self.commit('implementation')
        self.tip = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')

    def receive(self):
        return subprocess.run([sys.executable, '-B', '-m', 'shiftboss.hooks.pre_receive'],
                              cwd=self.repo, text=True, capture_output=True,
                              input=f'{self.base} {self.tip} refs/heads/codex/one\n')

    def test_business_ignores_narrow_scope_and_legacy_protected_paths(self):
        self.incoming(['engine/other/value.ts', 'engine/tools/generator.ts', 'engine/frozen.ts'])
        result = self.receive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([sys.executable, '-B', '-m', 'shiftboss.merge', 'codex/one'],
                                cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('value = 2', (self.repo / 'engine/other/value.ts').read_text())

    def test_check_source_alias_cannot_be_self_authorized(self):
        self.incoming(['scripts/check.mts'], self_grant=True)
        result = self.receive()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('改检查', result.stderr)
        self.assertIn('scripts/check.mts', result.stderr)

    def test_check_category_grants_all_checks_without_path_list(self):
        self.incoming(['scripts/check.mts', 'layout/screen.json'], allowed=True)
        result = self.receive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_test_directory_is_check_code_even_without_registration(self):
        self.incoming(['engine/tests/helper.ts'])
        result = self.receive()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('engine/tests/helper.ts', result.stderr)


class PermissionClassification(unittest.TestCase):
    def test_push_and_merge_classify_the_same_check_paths(self):
        from shiftboss import merge
        from shiftboss.hooks import pre_receive
        settings = {'merge': {'assertion_dirs': ['tests'],
                              'fast_commands': [{'run': ['node', 'scripts/check.mts']}]},
                    'verify': {'check_paths': ['tools/helper.mjs']},
                    'guard': {'layout_paths': ['layout/'], 'protected': ['business/']}}
        paths = ['business/editor.ts', 'tests/helper.ts', 'scripts/check.mts',
                 'tools/helper.mts', 'layout/screen.json', 'elsewhere/value-baseline.json']
        expected = paths[1:]
        self.assertEqual(check_hits(paths, settings), expected)
        with patch('shiftboss.config.load', return_value=settings):
            self.assertEqual(merge.check_hits(paths), expected)
        def git(*args):
            if args[0] == 'show':
                return Mock(returncode=0, stdout='{"改检查": "否"}')
            return Mock(returncode=0, stdout='\n'.join(paths))
        with patch.object(pre_receive, 'git', side_effect=git):
            self.assertEqual(pre_receive.check_trespass('one', 'tip', settings), expected)

    def test_legacy_scope_can_be_saved_without_parent_path_analysis(self):
        book = Mock()
        with (patch.object(taskbook.Taskbook, 'find', return_value=book),
              patch('shiftboss.config.load', side_effect=AssertionError('no path analysis')),
              redirect_stdout(io.StringIO())):
            self.assertEqual(taskbook.main(['set', 'one', '--field', '写入范围=["engine/"]']), 0)

    def test_dispatch_does_not_scan_contract_paths(self):
        book = Mock()
        book.contract_text.return_value = '- 要求：修改 `engine/tools/a.py` 和 `game/one.json`。'
        book.meta.side_effect = AssertionError('no path list analysis')
        with patch('shiftboss.config.load', side_effect=AssertionError('no path analysis')):
            self.assertIsNone(dispatch.contract_scope_reason(book))
