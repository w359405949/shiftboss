"""Grouped reminders retain per-task followup without hiding newly delivered branches."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from shiftboss import brief, todo


class GroupedReminders(unittest.TestCase):
    def test_add_persists_one_normalized_group_and_each_task_sees_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('shiftboss.config.control_dir', return_value=root):
                ident = todo.add(root, 'Exact remaining inputs', 'after:8h', ' alpha, beta,alpha ')
                self.assertEqual(len(todo.load(root)), 1)
                self.assertEqual(todo.load(root)[0]['line'], 'alpha,beta')
                for name in ['alpha', 'beta']:
                    self.assertTrue(todo.line_waits(root, root, name)[0].startswith(ident + ' '))
                self.assertEqual(todo.line_waits(root, root, 'alpha-extra'), [])
                todo.done(root, ident)
                self.assertEqual(todo.line_waits(root, root, 'beta'), [])

    def test_legacy_single_task_and_due_group_keep_existing_semantics(self):
        rows = [{'id': 'T1', 'line': 'alpha', 'when': f'at:{int(time.time()) + 3600}'},
                {'id': 'T2', 'line': 'alpha,beta', 'when': 'at:1'}]
        with patch.object(todo, 'load', return_value=rows):
            self.assertEqual(len(todo.line_waits(Path('/repo'), Path('/remote'), 'alpha')), 1)
            self.assertEqual(todo.line_waits(Path('/repo'), Path('/remote'), 'beta'), [])

    def summary(self, actual_dependency=False):
        root = Path('/repo')
        books = {name: SimpleNamespace(name=name, meta=lambda: {}, delivery_text=lambda: '执行状态：停下')
                 for name in ['alpha', 'beta']}
        statuses = [('alpha', '待派', '/copy-a', '停着', '可核对（停下）', ''),
                    ('beta', '待派', '/copy-b', '停着', '已合并', '')]
        grouped = [{'id': 'T1', 'line': 'alpha,beta', 'when': f'at:{int(time.time()) + 3600}'}]
        output = StringIO()
        with ExitStack() as stack:
            def mock(target, **kwargs):
                return stack.enter_context(patch(target, **kwargs))
            mock('shiftboss.brief.project_root', return_value=root)
            mock('shiftboss.brief.inbox.origin_git_dir', return_value=Path('/remote'))
            mock('shiftboss.brief.inbox.status_rows', return_value=(statuses, {}, [], []))
            mock('shiftboss.taskbook.Taskbook.find', side_effect=lambda repo, name: books.get(name))
            mock('shiftboss.taskbook.unmet_dependencies', side_effect=lambda repo, book, key:
                 ['real prerequisite'] if actual_dependency and book.name == 'alpha' else [])
            mock('shiftboss.taskbook.execution_status', return_value='停下')
            mock('shiftboss.todo.load', return_value=grouped)
            mock('shiftboss.brief.decisions.pending', return_value=[])
            mock('shiftboss.brief.branch_merged', return_value=True)
            mock('shiftboss.dispatch.read_quota', return_value=20)
            mock('shiftboss.automerge.awaiting', return_value={})
            mock('shiftboss.automerge.unreviewed', return_value=[])
            mock('shiftboss.idle_audit.pending', return_value=None)
            for name in ['phase_lines', 'mistake_lines', 'todo_lines', 'verify_lines', 'stale_lanes',
                         'draining_lanes', 'ratchet_lines', 'bell_lines', 'recovery_lines',
                         'machine_lines', 'docs_drift']:
                mock('shiftboss.brief.' + name, return_value=[])
            mock('shiftboss.brief.switch', return_value='开')
            mock('shiftboss.brief.mark_lanes', side_effect=lambda lines, repo: lines)
            for name in ['page_lines', 'periodic_lines', 'slowest_lines']:
                mock('shiftboss.patrol.' + name, return_value=[])
            with redirect_stdout(output):
                self.assertEqual(brief.main(), 0)
        return output.getvalue()

    def test_new_delivery_still_needs_review_while_group_reminder_is_future(self):
        text = self.summary()
        self.assertIn('【待核对】1', text)
        self.assertIn('- alpha｜停下', text)
        self.assertIn('beta｜停下｜待续派｜等 T1 ', text)
        actions = next(line for line in text.splitlines() if line.startswith('【该你处理】'))
        self.assertIn('待核对 1', actions)
        self.assertNotIn('待续派/收尾', actions)

    def test_real_taskbook_dependency_still_blocks_delivery_review(self):
        text = self.summary(actual_dependency=True)
        self.assertIn('【待核对】0', text)
        self.assertIn('real prerequisite', text)
        self.assertIn('分支待核', text)


if __name__ == '__main__':
    unittest.main()
