"""Compiler silence must not trigger the custom-tool idle watchdog."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import merge, task_checks


class FastCommandTimeout(unittest.TestCase):
    def run_quiet_child(self, *, disabled):
        original_wait = subprocess.Popen.wait

        def short_poll(process, timeout=None):
            return original_wait(process, timeout=None if timeout is None else min(timeout, 0.01))

        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(merge, 'REPO', Path(directory)),
                  patch.object(merge, 'FAST_IDLE_KILL', 0.02),
                  patch.object(subprocess.Popen, 'wait', short_poll)):
                command = [sys.executable, '-c',
                           'import sys,time;time.sleep(0.12);print("compiler diagnostic");'
                           'print("stderr diagnostic",file=sys.stderr);sys.exit(7)']
                if disabled:
                    return merge.run_until_idle(command, idle_timeout_seconds=0)
                return merge.run_until_idle(command)

    def test_explicit_zero_keeps_quiet_compiler_alive_and_preserves_failure_output(self):
        code, output = self.run_quiet_child(disabled=True)
        self.assertEqual(code, 7)
        self.assertIn('compiler diagnostic', output)
        self.assertIn('stderr diagnostic', output)
        self.assertNotIn('判为卡死', output)

    def test_default_still_terminates_a_silent_custom_tool(self):
        code, output = self.run_quiet_child(disabled=False)
        self.assertEqual(code, 124)
        self.assertIn('判为卡死', output)

    def test_registry_rejects_invalid_timeout_values(self):
        for invalid in (-1, False, True, None, '0', 1.5):
            with self.subTest(value=invalid):
                item = {'name': 'compiler', 'run': ['compiler'], 'idle_timeout_seconds': invalid}
                with (patch('shiftboss.config.fast_commands', return_value=[item]),
                      self.assertRaises(task_checks.TaskChecksError)):
                    task_checks.registry(Path('.'))

    def test_delivery_runner_forwards_explicit_zero_and_rejects_real_nonzero(self):
        item = {'name': 'compiler', 'run': ['compiler'], 'idle_timeout_seconds': 0}
        with (patch('shiftboss.background_commands.commands', return_value=[]),
              patch.object(task_checks, 'commands', return_value=(None, [item])),
              patch.object(merge, 'run_until_idle', return_value=(2, 'real type error')) as run,
              redirect_stdout(io.StringIO()), self.assertRaisesRegex(merge.MergeError, 'real type error')):
            merge.run_fast_commands(['src/a.ts'])
        run.assert_called_once_with(['compiler'], idle_timeout_seconds=0)

    def test_unconfigured_custom_tool_retains_default_runner_policy(self):
        item = {'name': 'custom', 'run': ['custom']}
        with (patch('shiftboss.background_commands.commands', return_value=[]),
              patch.object(task_checks, 'commands', return_value=(None, [item])),
              patch.object(merge, 'run_until_idle', return_value=(0, '')) as run,
              redirect_stdout(io.StringIO())):
            merge.run_fast_commands(['src/a.ts'])
        run.assert_called_once_with(['custom'])

    def test_explicit_compiler_wait_does_not_request_automatic_background_migration(self):
        item = {'name': 'compiler', 'run': ['compiler'], 'idle_timeout_seconds': 0}
        output = io.StringIO()
        with (patch('shiftboss.background_commands.commands', return_value=[]),
              patch.object(task_checks, 'commands', return_value=(None, [item])),
              patch.object(merge, 'run_until_idle', return_value=(0, '')),
              patch.object(merge.time, 'monotonic', side_effect=[0, 120]),
              redirect_stdout(output)):
            merge.run_fast_commands(['src/a.ts'])
        self.assertIn('120.000s', output.getvalue())
        self.assertNotIn('移入常驻验证', output.getvalue())

    def test_complete_compiler_failure_is_available_to_the_delivery_log(self):
        item = {'name': 'compiler', 'run': ['compiler'], 'idle_timeout_seconds': 0}
        diagnostics = ''.join(f'file-{index}.ts(1,1): error TS2322: exact diagnostic {index}\n'
                              for index in range(40))
        with (patch('shiftboss.background_commands.commands', return_value=[]),
              patch.object(task_checks, 'commands', return_value=(None, [item])),
              patch.object(merge, 'run_until_idle', return_value=(2, diagnostics)),
              self.assertRaises(merge.MergeError) as caught):
            merge.run_fast_commands(['src/a.ts'])
        self.assertEqual(str(caught.exception), '快速检查 compiler 失败（退出码 2）：\n' + diagnostics)

    def test_custom_tool_failure_retains_its_existing_bounded_summary(self):
        item = {'name': 'custom', 'run': ['custom']}
        diagnostics = ''.join(f'line-{index}\n' for index in range(40))
        with (patch('shiftboss.background_commands.commands', return_value=[]),
              patch.object(task_checks, 'commands', return_value=(None, [item])),
              patch.object(merge, 'run_until_idle', return_value=(2, diagnostics)),
              self.assertRaises(merge.MergeError) as caught):
            merge.run_fast_commands(['src/a.ts'])
        self.assertNotIn('line-0\n', str(caught.exception))
        self.assertIn('line-28\n', str(caught.exception))
        self.assertIn('line-39', str(caught.exception))
