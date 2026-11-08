"""检查清单限定开发线额外取证，不把声明变成每次交付的重活。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import merge, taskbook
from shiftboss.task_checks import commands, describe


class TaskCheckScope(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        taskbook.new(self.root, 'transcribe', kind='task')
        self.book = taskbook.Taskbook.find(self.root, 'transcribe')
        self.book.set_meta(**{'快速检查': []})

    def plan(self):
        with (patch('shiftboss.task_checks.resolve', return_value=self.book),
              patch('shiftboss.task_checks.registry', return_value={})):
            return describe(self.root, 'transcribe')

    def test_new_task_defaults_to_no_extra_checks(self):
        self.assertEqual(self.book.meta()['专项检查'], [])
        text = self.plan()
        self.assertIn('无；仅执行交付快检', text)
        self.assertIn('完成标准与已有红不自动增加检查', text)

    def test_legacy_missing_field_does_not_authorize_extra_checks(self):
        meta = self.book.meta()
        del meta['专项检查']
        (self.book.path / 'meta.json').write_text(json.dumps(meta))
        self.assertIn('旧任务未声明，按不追加处理', self.plan())

    def test_set_preserves_script_arguments_and_scope(self):
        item = 'engine/tools/receipt.mts --files src/a.ts src/b.ts；每自然批一次'
        with (patch.object(taskbook, 'project_root', return_value=self.root),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(taskbook.main(['set', 'transcribe', '--field',
                             '专项检查=' + json.dumps([item])]), 0)
        self.assertEqual(self.book.meta()['专项检查'], [item])
        self.assertIn(item, self.plan())

    def test_invalid_extra_plan_is_rejected_before_metadata_changes(self):
        before = self.book.meta()
        for value in ('null', 'false', '{}', '"script with spaces"', '[""]', '[3]', '["a","a"]'):
            with (self.subTest(value=value),
                  patch.object(taskbook, 'project_root', return_value=self.root),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(taskbook.main(['set', 'transcribe', '--field', '专项检查=' + value]), 2)
                self.assertEqual(self.book.meta(), before)

    def test_listing_and_fast_delivery_do_not_execute_extra_command(self):
        marker = self.root / 'extra-ran'
        self.book.set_meta(**{'专项检查': [f'python -c "from pathlib import Path; Path({str(marker)!r}).touch()"']})
        with (patch('shiftboss.task_checks.resolve', return_value=self.book),
              patch('shiftboss.task_checks.registry', return_value={}),
              patch.object(merge, 'REPO', self.root)):
            self.assertIn('不由交付快检自动运行', describe(self.root, 'transcribe'))
            self.assertEqual(commands(self.root, 'transcribe')[1], [])
            merge.run_fast_commands(['src/a.ts'], task='transcribe')
        self.assertFalse(marker.exists())

    def test_invalid_stored_plan_does_not_fall_back_to_running_everything(self):
        self.book.set_meta(**{'专项检查': False})
        with (patch('shiftboss.task_checks.resolve', return_value=self.book),
              patch('shiftboss.task_checks.registry') as registry,
              self.assertRaises(ValueError)):
            commands(self.root, 'transcribe')
        registry.assert_not_called()
