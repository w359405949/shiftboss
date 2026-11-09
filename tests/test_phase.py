from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import brief, config, phase


class CurrentPhase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.control = self.root / 'control'
        self.control.mkdir()
        self.path = self.control / phase.NAME

    def test_brief_reads_current_title_goal_and_document_without_dumping_strategy(self):
        self.path.write_text('# TS 转写快速推进\n目标：按自然批次交付。\n\n内部取舍不堆进简报。\n')
        with patch.object(config, 'control_dir', return_value=self.control):
            lines = brief.phase_lines(self.root / 'hub.git')
            self.assertEqual(lines[0], '【当前阶段】TS 转写快速推进')
            self.assertIn('- 目标｜按自然批次交付。', lines)
            self.assertIn(str(self.path), lines[-1])
            self.assertNotIn('内部取舍', '\n'.join(lines))
            self.path.write_text('# 发布验收\n目标：核实际发布结果。\n')
            updated = '\n'.join(brief.phase_lines(self.root / 'hub.git'))
            self.assertIn('发布验收', updated)
            self.assertNotIn('TS 转写', updated)

    def test_clear_and_delete_remove_the_old_phase(self):
        self.path.write_text('# Old phase\n')
        self.assertEqual(phase.read(self.control).status, 'active')
        self.path.write_text(' \n')
        self.assertEqual(phase.summary(phase.read(self.control)), ['【当前阶段】：已清空'])
        self.path.unlink()
        self.assertEqual(phase.summary(phase.read(self.control)), ['【当前阶段】：未设置'])

    def test_progress_keeps_its_observation_time_and_rereads_updates(self):
        self.path.write_text('# 类型集成\n目标：并行收敛。\n'
                             '进度：约 5%–10%（估算）\n'
                             '更新时间：2026-11-09 23:57 +08:00\n'
                             '依据：已接收首轮集成批次，尚无修后全量结果。\n')
        with patch.object(config, 'control_dir', return_value=self.control):
            first = brief.phase_lines(self.root / 'hub.git')
            self.assertIn('- 进度｜约 5%–10%（估算）', first)
            self.assertIn('- 更新于｜2026-11-09 23:57 +08:00', first)
            self.assertIn('- 依据｜已接收首轮集成批次，尚无修后全量结果。', first)
            os.utime(self.path, (1, 1))
            self.assertEqual(brief.phase_lines(self.root / 'hub.git'), first)
            self.path.write_text('# 发布验收\n目标：核结果。\n'
                                 '进度: 约 20%（估算）\n'
                                 '更新时间: 2026-11-10 01:00 +08:00\n依据: 新一轮实际结果。\n')
            updated = brief.phase_lines(self.root / 'hub.git')
            self.assertIn('- 进度｜约 20%（估算）', updated)
            self.assertIn('- 更新于｜2026-11-10 01:00 +08:00', updated)
            self.assertNotIn('23:57', '\n'.join(updated))
            self.path.write_text('# 下一阶段\n目标：新目标。\n')
            self.assertNotIn('进度｜', '\n'.join(brief.phase_lines(self.root / 'hub.git')))

    def test_progress_without_time_is_explicit_and_summary_fields_are_bounded(self):
        self.path.write_text('# 阶段\n进度：' + '估' * 1000 + '\n依据：' + '据' * 1000)
        lines = phase.summary(phase.read(self.control))
        self.assertIn('- 更新于｜未填写（不是实时进度）', lines)
        self.assertTrue(all(len(line) < 200 for line in lines[:-1]))

    def test_focus_and_immediate_priority_remain_a_short_dispatcher_summary(self):
        self.path.write_text('# 类型集成\n目标：完成严格验收。\n'
                             '重心：公共类型对齐，各领域并行收敛。\n'
                             '当务之急：恢复检查链路。\n\n'
                             '内部执行策略和逐项任务不放简报。\n')
        lines = phase.summary(phase.read(self.control))
        self.assertIn('- 重心｜公共类型对齐，各领域并行收敛。', lines)
        self.assertIn('- 当务之急｜恢复检查链路。', lines)
        self.assertNotIn('- 目标｜完成严格验收。', lines)
        self.assertNotIn('内部执行策略', '\n'.join(lines))

    def test_invalid_or_oversized_document_is_visible_not_an_empty_goal(self):
        for raw in (b'\xff', b'x' * (phase.MAX_DOCUMENT_BYTES + 1)):
            with self.subTest(bytes=len(raw)):
                self.path.write_bytes(raw)
                current = phase.read(self.control)
                self.assertEqual(current.status, 'error')
                self.assertEqual(phase.summary(current)[0], '【当前阶段】：读取失败')
                self.assertNotIn('未设置', '\n'.join(phase.summary(current)))

    def test_control_escape_is_not_read(self):
        outside = self.root / 'outside.md'
        outside.write_text('outside-private-content')
        try:
            self.path.symlink_to(outside)
        except OSError as error:
            self.skipTest(str(error))
        current = phase.read(self.control)
        self.assertEqual(current.status, 'error')
        self.assertNotIn('outside-private-content', current.text)

    def test_brief_title_and_goal_are_bounded(self):
        self.path.write_text('# ' + '长' * 1000 + '\n目标：' + '多' * 1000)
        lines = phase.summary(phase.read(self.control))
        self.assertLess(len(lines[0]), 200)
        self.assertLess(len(lines[1]), 200)
        self.assertEqual(phase.read(self.control).text, self.path.read_text())

    def test_dispatcher_phase_helper_does_not_require_resident_restart(self):
        package = self.root / 'package'
        package.mkdir()
        for name, timestamp in [('__init__.py', 1), ('verify.py', 1), ('phase.py', 100)]:
            path = package / name
            path.write_text('')
            os.utime(path, (timestamp, timestamp))
        lock = self.root / 'worker.lock'
        lock.write_text('12')
        lanes = [('工具闸', lock, self.root / 'worker.stop')]
        with (patch('shiftboss.__file__', str(package / '__init__.py')),
              patch.object(brief, 'lane_paths', return_value=lanes),
              patch.object(brief.psutil, 'Process') as process):
            process.return_value.create_time.return_value = 10
            process.return_value.cmdline.return_value = ['verify', '--publish']
            self.assertEqual(brief.stale_lanes(self.root), [])
            os.utime(package / 'verify.py', (200, 200))
            self.assertEqual(brief.stale_lanes(self.root), ['工具闸'])


if __name__ == '__main__':
    unittest.main()
