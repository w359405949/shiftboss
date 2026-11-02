"""已受理的正常停止是等待状态；不隐藏其余可办事项或未停止的旧进程。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import brief


class ShutdownSummary(unittest.TestCase):
    def actions(self, **kwargs):
        return brief.actionable([], [], [], [], [], [], [], **kwargs)

    def test_stale_worker_without_stop_still_requires_restart(self):
        rows = self.actions(stale=['工具闸'])
        self.assertEqual(len(rows), 1)
        self.assertIn('stop 后 start --publish', rows[0])

    def test_only_draining_lanes_are_removed_from_restart_action(self):
        rows = self.actions(stale=['工具闸', '页面', '定时'], draining=['工具闸', '定时'])
        self.assertEqual(len(rows), 1)
        self.assertIn('（页面）', rows[0])
        self.assertNotIn('（工具闸', rows[0])

    def test_missing_publish_flag_waits_for_already_requested_stop(self):
        self.assertEqual(self.actions(stale=['工具闸没带 --publish'], draining=['工具闸']), [])
        self.assertTrue(self.actions(stale=['工具闸没带 --publish']))

    def test_other_actions_remain_visible_during_shutdown(self):
        rows = brief.actionable(['review'], [], [], ['- T1｜可做｜next'], [], [], [],
                                stale=['工具闸'], draining=['工具闸'])
        self.assertEqual(rows, ['待核对 1', '待办 T1'])

    def test_draining_requires_stop_and_live_pid_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [(name, root / (name + '.lock'), root / (name + '.stop'))
                    for name in ['工具闸', '页面', '定时']]
            for index, (_, lock, _) in enumerate(rows):
                lock.write_text(str(index + 10))
            rows[0][2].touch()
            rows[1][2].touch()
            with patch.object(brief, 'lane_paths', return_value=rows), patch.object(brief.psutil, 'pid_exists', side_effect=lambda pid: pid != 11):
                self.assertEqual(brief.draining_lanes(root), ['工具闸'])
                rows[0][1].unlink()
                self.assertEqual(brief.draining_lanes(root), [])
                rows[0][1].write_text('not-a-pid')
                self.assertEqual(brief.draining_lanes(root), [])


if __name__ == '__main__':
    unittest.main()
