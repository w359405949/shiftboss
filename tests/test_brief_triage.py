"""简报遵守 triage 的只读合同，不把归档当成业务修复。"""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shiftboss import brief


class DiagnosticPageSummary(unittest.TestCase):
    line = '- 页面红 3｜修复无效｜owner｜route-a、route-b、route-c'

    def classify(self, meta):
        book = SimpleNamespace(meta=lambda: meta) if meta is not None else None
        with patch('shiftboss.taskbook.Taskbook.find', return_value=book):
            return brief.diagnostic_page_lines(Path('/repo'), [self.line])

    def test_archived_readonly_triage_keeps_red_routes_without_failed_repair_action(self):
        rows = self.classify({'子类型': 'triage', '调度状态': '已归档（合并 abc1234）'})
        self.assertEqual(rows, ['- 页面红 3｜已分诊，未修复｜owner｜route-a、route-b、route-c'])
        self.assertEqual(brief.actionable([], [], [], [], [], [], rows), [])

    def test_real_repair_and_unknown_legacy_outcomes_remain_actionable(self):
        for meta in ({'子类型': 'repair', '调度状态': '已归档'},
                     {'子类型': 'task', '调度状态': '已归档'},
                     {'调度状态': '已归档'}, None):
            with self.subTest(meta=meta):
                rows = self.classify(meta)
                self.assertEqual(rows, [self.line])
                self.assertEqual(brief.actionable([], [], [], [], [], [], rows), ['页面红无主或修复无效 1 组'])

    def test_active_triage_is_not_treated_as_completed(self):
        self.assertEqual(self.classify({'子类型': 'triage', '调度状态': '待派'}), [self.line])

    def test_unowned_new_red_and_other_work_are_never_hidden(self):
        lines = ['- 页面红 1｜无人负责｜new-route', '- 工具闸红｜source｜无人负责']
        with patch('shiftboss.taskbook.Taskbook.find') as find:
            self.assertEqual(brief.diagnostic_page_lines(Path('/repo'), lines), lines)
            find.assert_not_called()
        self.assertEqual(brief.actionable(['next'], [], [], [], [], [], lines[:1]),
                         ['待核对 1', '页面红无主或修复无效 1 组'])


if __name__ == '__main__':
    unittest.main()
