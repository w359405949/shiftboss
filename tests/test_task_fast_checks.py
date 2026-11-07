"""每条任务只运行自己声明且匹配的脚本，错误不会回退成全跑或跳过。"""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import check, config, merge, taskbook
from shiftboss.task_checks import (TaskChecksError, describe, registry, selected,
                                  task_for_target)


class TaskFastChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('remote', 'add', 'origin', str(self.root))
        self.git('config', 'user.name', 'Fast checks fixture')
        self.git('config', 'user.email', 'fixture@localhost')
        self.items = [
            {'name': 'source', 'run': [sys.executable, 'source_check.py'], 'when': ['src/']},
            {'name': 'docs', 'run': [sys.executable, 'docs_check.py'], 'when': ['docs/']},
            {'name': 'background', 'run': [sys.executable, 'background_check.py'], 'background': True},
        ]
        for kind in ('source', 'docs', 'background'):
            (self.root / f'{kind}_check.py').write_text(
                f'from pathlib import Path\nPath("{kind}.ran").touch()\n')
        for name, choices in [('alpha', ['source']), ('beta', ['docs'])]:
            taskbook.new(self.root, name, kind='task')
            taskbook.Taskbook.find(self.root, name).set_meta(**{'快速检查': choices})
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.addCleanup(config.load.cache_clear)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def environment(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(config, 'fast_commands', return_value=self.items))
        stack.enter_context(patch.object(merge, 'REPO', self.root))
        return stack

    def book(self, name='alpha'):
        return taskbook.Taskbook.find(self.root, name)

    def test_two_tasks_run_only_their_own_matching_command(self):
        with self.environment():
            merge.run_fast_commands(['src/one.ts', 'docs/one.md'], task='alpha')
            self.assertTrue((self.root / 'source.ran').exists())
            self.assertFalse((self.root / 'docs.ran').exists())
            self.assertFalse((self.root / 'background.ran').exists())
            merge.run_fast_commands(['src/one.ts', 'docs/one.md'], task='beta')
            self.assertTrue((self.root / 'docs.ran').exists())
            merge.run_fast_commands(['docs/one.md'], task='alpha')
            self.assertFalse((self.root / 'background.ran').exists())

    def test_missing_unknown_duplicate_and_background_refuse_without_execution(self):
        with self.environment():
            for value in (None, ['missing'], ['source', 'source'], ['background'], False):
                name = 'alpha'
                if value is None:
                    taskbook.new(self.root, 'unconfigured', kind='task')
                    name = 'unconfigured'
                else:
                    self.book().set_meta(**{'快速检查': value})
                with self.subTest(value=value), self.assertRaises(TaskChecksError):
                    merge.run_fast_commands(['src/one.ts'], task=name)
            self.assertFalse(any(self.root.glob('*.ran')))

    def test_empty_manifest_keeps_builtin_guards_and_background_registration(self):
        self.book().set_meta(**{'快速检查': []})
        with self.environment():
            merge.run_fast_commands(['src/one.ts'], task='alpha')
            self.assertFalse(any(self.root.glob('*.ran')))
            from shiftboss.background_commands import commands
            self.assertEqual(commands(self.root), [self.items[2]['run']])
            with (patch.object(merge, 'generation_plan', side_effect=merge.MergeError('builtin rejected')),
                  self.assertRaisesRegex(merge.MergeError, 'builtin rejected')):
                merge.fast_checks('HEAD', task='alpha')

    def test_failure_preserves_original_exit_and_error(self):
        (self.root / 'source_check.py').write_text('import sys\nprint("ORIGINAL-ERROR")\nsys.exit(7)\n')
        with self.environment(), self.assertRaisesRegex(merge.MergeError, '退出码 7.*') as failed:
            merge.run_fast_commands(['src/one.ts'], task='alpha')
        self.assertIn('ORIGINAL-ERROR', str(failed.exception))

    def test_list_and_taskbook_view_share_real_argv_without_running(self):
        self.git('switch', '-q', '-c', 'codex/alpha')
        (self.root / 'src').mkdir()
        (self.root / 'src/one.ts').write_text('export const value = 1;\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'change')
        captured = io.StringIO()
        with (self.environment(), patch.object(check, 'REPO', self.root),
              patch.object(taskbook, 'project_root', return_value=self.root),
              contextlib.redirect_stdout(captured)):
            self.assertEqual(check.main(['--list']), 0)
            plan = describe(self.root, 'alpha')
            self.assertIn('source_check.py', plan)
            self.assertNotIn('docs_check.py', plan)
            self.assertEqual(taskbook.main(['show', 'alpha', '--checks']), 0)
            self.assertEqual(check.main(['--task', 'beta', '--list']), 1)
            self.assertEqual(taskbook.main(['set', 'alpha', '--field', '快速检查=["missing"]']), 2)
            self.assertEqual(self.book().meta()['快速检查'], ['source'])
        self.assertIn('source_check.py', captured.getvalue())
        self.assertFalse(any(self.root.glob('*.ran')))

    def test_target_uses_source_task_and_ambiguous_intermediate_requires_name(self):
        self.git('switch', '-q', '-c', 'codex/alpha')
        (self.root / 'one').write_text('one')
        self.git('add', '.')
        self.git('commit', '-qm', 'one')
        shared = self.git('rev-parse', 'HEAD')
        self.git('branch', 'codex/beta')
        self.git('commit', '--allow-empty', '-qm', 'two')
        last = self.git('rev-parse', 'HEAD')
        with self.environment():
            self.assertEqual(task_for_target(self.root, last).name, 'alpha')
            # Exact tip beta wins even though alpha also contains shared.
            self.assertEqual(task_for_target(self.root, shared).name, 'beta')
            self.git('branch', '-f', 'codex/beta', last)
            with self.assertRaises(TaskChecksError):
                task_for_target(self.root, shared)
            self.assertEqual(task_for_target(self.root, shared, 'alpha').name, 'alpha')
            with self.assertRaises(TaskChecksError):
                task_for_target(self.root, shared, 'absent')

    def test_dispatch_and_merge_frontend_reject_bad_plan(self):
        # Same description/selection function is used by dispatch and fast_checks;
        # merge must reject before calling any builtin or custom command.
        taskbook.new(self.root, 'unconfigured', kind='task')
        with self.environment():
            with self.assertRaises(TaskChecksError):
                describe(self.root, 'unconfigured')
            with patch.object(merge, 'generation_plan') as builtin:
                with self.assertRaises(TaskChecksError):
                    merge.fast_checks('HEAD', task='unconfigured')
                builtin.assert_not_called()

    def test_registry_invalid_item_never_becomes_empty_success(self):
        with self.environment():
            self.items.append(self.items[0].copy())
            with self.assertRaises(TaskChecksError):
                registry(self.root)

    def prepare_merge(self):
        from shiftboss.outputs import CATALOG, catalog_document
        rows = ['[regen]', 'plan = "regen.json"', 'catalog = "derived-artifacts.json"']
        for item in self.items:
            rows += ['[[merge.fast_commands]]', 'name = ' + json.dumps(item['name']),
                     'run = ' + json.dumps(item['run'])]
            if item.get('when'):
                rows.append('when = ' + json.dumps(item['when']))
            if item.get('background'):
                rows.append('background = true')
        (self.root / 'shiftboss.toml').write_text('\n'.join(rows) + '\n')
        command = [sys.executable, '-c', 'raise SystemExit(99)']
        (self.root / 'regen.json').write_text(json.dumps({'steps': [command], 'gates': [command]}))
        (self.root / 'derived.json').write_text('{}')
        (self.root / CATALOG).write_text(json.dumps(catalog_document([
            {'command': command, 'files': ['derived.json']}])) )
        (self.root / '.gitignore').write_text('*.ran\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'configured checks')
        self.git('switch', '-q', '-c', 'codex/alpha')
        (self.root / 'src').mkdir()
        (self.root / 'src/change.txt').write_text('changed')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/change.md').write_text('changed')
        self.git('add', '.')
        self.git('commit', '-qm', 'source and documentation')
        target = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        return target

    def merge_process(self, target):
        return subprocess.run([sys.executable, '-B', '-m', 'shiftboss.merge', target],
                              cwd=self.root, text=True, capture_output=True)

    def test_real_merge_uses_the_same_task_selection_in_fresh_finish_process(self):
        target = self.prepare_merge()
        result = self.merge_process(target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / 'source.ran').is_file())
        self.assertFalse((self.root / 'docs.ran').exists())
        self.assertFalse((self.root / 'background.ran').exists())
        self.assertIn('任务书清单', result.stdout)
        self.assertEqual(self.git('status', '--porcelain'), '')
        original_mtime = (self.root / 'source.ran').stat().st_mtime_ns
        self.git('branch', '-f', 'codex/alpha', 'HEAD')
        self.git('branch', '-f', 'codex/beta', 'HEAD')
        again = self.merge_process(target)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual((self.root / 'source.ran').stat().st_mtime_ns, original_mtime)

    def test_real_merge_refuses_unknown_check_before_modifying_git_tree(self):
        target = self.prepare_merge()
        self.book().set_meta(**{'快速检查': ['unknown']})
        self.git('add', '.')
        self.git('commit', '-qm', 'invalid task selection')
        before = self.git('rev-parse', 'HEAD')
        result = self.merge_process(target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('快速检查未登记', result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertFalse((self.root / '.git/MERGE_HEAD').exists())
        self.assertFalse(any(self.root.glob('*.ran')))

    def test_remote_task_tip_takes_precedence_over_unpublished_local_commit(self):
        old = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-c', 'codex/alpha')
        self.git('commit', '--allow-empty', '-qm', 'unpublished')
        proposed = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/codex/alpha', old)
        with self.environment():
            with self.assertRaisesRegex(TaskChecksError, '远端'):
                task_for_target(self.root, proposed)

    def test_actual_dispatch_refuses_missing_manifest_before_launch(self):
        (self.root / 'shiftboss.toml').write_text('[project]\n')
        taskbook.new(self.root, 'unconfigured', kind='task')
        book = taskbook.Taskbook.find(self.root, 'unconfigured')
        book.set_meta(**{'调度状态': '待派', '你的工作副本': str(self.root),
                         '完成标准': '成形', '破坏性': '否'})
        result = subprocess.run([sys.executable, '-m', 'shiftboss.dispatch',
                                 'unconfigured', '--dry-run'], cwd=self.root,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('未声明「快速检查」', result.stdout)
        self.assertEqual(book.meta()['轮次'], 0)
