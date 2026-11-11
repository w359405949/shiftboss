import subprocess
import unittest
from unittest.mock import patch

from shiftboss import inbox


class StartedSignsTests(unittest.TestCase):
    def read_signs(self, revision, status):
        with patch.object(inbox.subprocess, 'run', side_effect=[revision, status]) as run, \
                patch.object(inbox, 'stale_index_lock', return_value=''):
            result = inbox.started_signs('/copy', 'codex/task')
        for call in run.call_args_list:
            self.assertIn('--no-optional-locks', call.args[0])
        return result

    def test_dirty_worktree(self):
        result = self.read_signs(subprocess.CompletedProcess([], 0, 'abc\n'),
                                 subprocess.CompletedProcess([], 0, ' M one.ts\n M two.ts\n'))
        self.assertEqual(result, '目标分支已建、2 个脏文件')

    def test_confirmed_clean_worktree(self):
        result = self.read_signs(subprocess.CompletedProcess([], 0, 'abc\n'),
                                 subprocess.CompletedProcess([], 0, ''))
        self.assertEqual(result, '目标分支已建、工作树干净')

    def test_status_timeout_is_unknown(self):
        result = self.read_signs(subprocess.CompletedProcess([], 0, 'abc\n'),
                                 subprocess.TimeoutExpired('git', 20))
        self.assertEqual(result, '目标分支已建、工作树状态未取得')

    def test_failed_status_is_unknown(self):
        result = self.read_signs(subprocess.CompletedProcess([], 0, 'abc\n'),
                                 subprocess.CompletedProcess([], 128, '', 'fatal'))
        self.assertEqual(result, '目标分支已建、工作树状态未取得')

    def test_branch_timeout_does_not_suggest_restart(self):
        result = self.read_signs(subprocess.TimeoutExpired('git', 20),
                                 subprocess.CompletedProcess([], 0, ''))
        self.assertEqual(result, '分支状态未取得、工作树干净')

    def test_failed_branch_read_is_unknown(self):
        result = self.read_signs(subprocess.CompletedProcess([], 128, '', 'fatal'),
                                 subprocess.CompletedProcess([], 0, ''))
        self.assertEqual(result, '分支状态未取得、工作树干净')

    def test_confirmed_missing_branch_and_clean_worktree(self):
        result = self.read_signs(subprocess.CompletedProcess([], 1, ''),
                                 subprocess.CompletedProcess([], 0, ''))
        self.assertIn('目标分支未建、工作树干净——多半没起来', result)


if __name__ == '__main__':
    unittest.main()
