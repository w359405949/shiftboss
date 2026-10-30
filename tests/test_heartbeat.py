"""固定心跳不受事件流影响，且监听与周期能跨多次响铃持续。"""
from contextlib import ExitStack
from datetime import datetime
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shiftboss import brief, doorbell


class StopClock(Exception):
    pass


class FixedHeartbeat(unittest.TestCase):
    def watch_until(self, path, clock, stop, minutes=1, jump=None):
        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock[0], tz)

        def events(_path):
            with _path.open('a') as handle:
                handle.write(f'{ClockDateTime.now().isoformat()} codex/active-line\n')

        def tick(_seconds):
            clock[0] = jump(clock[0]) if jump else clock[0] + 10
            if clock[0] >= stop:
                raise StopClock

        with ExitStack() as stack:
            stack.enter_context(patch.object(doorbell, 'acquire', return_value=io.StringIO()))
            stack.enter_context(patch.object(doorbell, 'datetime', ClockDateTime))
            stack.enter_context(patch.object(doorbell.time, 'time', side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(doorbell.time, 'monotonic', side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(doorbell.time, 'sleep', side_effect=tick))
            stack.enter_context(patch.object(doorbell, 'ring_ripe_todos', side_effect=events))
            for name in ('say', 'say_stalled', 'say_puzzles', 'ring_idle_audit'):
                stack.enter_context(patch.object(doorbell, name))
            with self.assertRaises(StopClock):
                doorbell.watch(path, minutes, path)

    def heartbeats(self, path):
        return [doorbell._event_time(line) for line in path.read_text().splitlines()
                if line.split()[-1:] == ['heartbeat']]

    def test_busy_listener_keeps_running_and_heartbeats_stay_periodic(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bell.log'
            path.touch()
            self.watch_until(path, [1000], 1130)
            self.assertEqual(self.heartbeats(path), [1060, 1120])
            self.assertGreater(path.read_text().count('codex/active-line'), 2)

    def test_restart_preserves_cycle_instead_of_rearming(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bell.log'
            path.touch()
            clock = [1000]
            self.watch_until(path, clock, 1070)
            self.assertEqual(self.heartbeats(path), [1060])
            self.watch_until(path, clock, 1130)
            self.assertEqual(self.heartbeats(path), [1060, 1120])
            self.assertEqual(doorbell.heartbeat_schedule(path), (1000, 60))

    def test_slow_delivery_skips_missed_ticks_without_drifting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bell.log'
            path.touch()
            self.watch_until(path, [1000], 1250,
                             jump=lambda now: 1190 if now == 1060 else now + 10)
            self.assertEqual(self.heartbeats(path), [1060, 1190, 1240])
            self.assertEqual(doorbell.next_heartbeat_at(1000, 60, 1190), 1240)

    def test_migration_uses_last_heartbeat_not_newer_event(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bell.log'
            path.write_text(f'{datetime.fromtimestamp(1000).isoformat()} heartbeat\n'
                            f'{datetime.fromtimestamp(1055).isoformat()} codex/new-push\n')
            anchor, seconds = doorbell.heartbeat_schedule(path, 1, 1055)
            self.assertEqual((anchor, seconds), (1000, 60))
            self.assertEqual(doorbell.next_heartbeat_at(anchor, seconds, 1055), 1060)

    def test_disabled_heartbeat_still_processes_events(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bell.log'
            path.touch()
            self.watch_until(path, [1000], 1130, minutes=0)
            self.assertEqual(self.heartbeats(path), [])
            self.assertGreater(path.read_text().count('codex/active-line'), 2)
            self.assertIsNone(doorbell.next_heartbeat_at(*doorbell.heartbeat_schedule(path), 1100))

    def test_brief_uses_same_schedule_and_respects_disabled_setting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bell.log'
            path.touch()
            doorbell.heartbeat_schedule(path, 1, 1000)
            path.write_text(f'{datetime.fromtimestamp(1059).isoformat()} codex/new-push\n')
            with (patch.object(doorbell, 'default_path', return_value=path),
                  patch.object(doorbell, 'rival_listeners', return_value=[SimpleNamespace(create_time=lambda: 1000)]),
                  patch.object(brief.time, 'time', return_value=1059)):
                self.assertEqual(brief.bell_lines(), [f'- 监听｜在｜固定心跳 {brief.clock(1060)}'])
                doorbell.heartbeat_schedule(path, 0, 1059)
                self.assertEqual(brief.bell_lines(), ['- 监听｜在｜心跳关闭'])


if __name__ == '__main__':
    unittest.main()
