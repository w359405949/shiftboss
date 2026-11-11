"""Git 提交自动使用已有快检选择与失败判定，不执行项目编译。"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import config, merge
from shiftboss.hooks import pre_commit


class PreCommitFastChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.root)], check=True)
        self.addCleanup(config.load.cache_clear)

    def stage(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '--', name], check=True)

    def hook(self):
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.chdir(self.root))
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(merge, 'REPO', self.root))
        return stack

    def test_source_commit_automatically_calls_registered_checks(self):
        self.stage('src/one.ts')
        with self.hook(), patch.object(merge, 'run_fast_commands') as run:
            self.assertEqual(pre_commit.main(), 0)
        run.assert_called_once_with(['src/one.ts'])

    def test_nonzero_check_rejects_commit_and_preserves_error(self):
        self.stage('src/one.ts')
        captured = io.StringIO()
        with (self.hook(), contextlib.redirect_stderr(captured),
              patch.object(merge, 'run_fast_commands', side_effect=merge.MergeError('original tsc error'))):
            self.assertEqual(pre_commit.main(), 1)
        self.assertIn('original tsc error', captured.getvalue())

    def test_report_only_commit_does_not_run_source_compiler(self):
        self.stage('shiftboss/plans/alpha/delivery.md')
        items = [{'name': 'compiler', 'run': ['MUST-NOT-RUN'], 'when': ['src/']}]
        with (self.hook(), patch.object(config, 'fast_commands', return_value=items),
              patch.object(merge, 'run_until_idle') as run):
            self.assertEqual(pre_commit.main(), 0)
        run.assert_not_called()

    def test_unstaged_source_does_not_change_commit_trigger(self):
        (self.root / 'unstaged.ts').write_text('fixture')
        with self.hook(), patch.object(merge, 'run_fast_commands') as run:
            self.assertEqual(pre_commit.main(), 0)
        run.assert_not_called()

    def test_workflow_merge_already_checks_and_does_not_compile_twice(self):
        self.stage('src/one.ts')
        with (self.hook(), patch.dict(os.environ, {'MM_WORKFLOW_MERGE': '1'}),
              patch.object(merge, 'run_fast_commands') as run):
            self.assertEqual(pre_commit.main(), 0)
        run.assert_not_called()

    def test_merge_lock_still_rejects_before_checks(self):
        self.stage('src/one.ts')
        (self.root / '.git/shiftboss-merge-in-flight').write_text('in-flight')
        with (self.hook(), contextlib.redirect_stderr(io.StringIO()),
              patch.object(merge, 'run_fast_commands') as run):
            self.assertEqual(pre_commit.main(), 1)
        run.assert_not_called()

    def test_check_setup_errors_are_not_fail_open(self):
        self.stage('src/one.ts')
        with (self.hook(), contextlib.redirect_stderr(io.StringIO()),
              patch.object(merge, 'run_fast_commands', side_effect=ValueError('invalid registry'))):
            self.assertEqual(pre_commit.main(), 1)


if __name__ == '__main__':
    unittest.main()
