import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from shiftboss import dispatch, state


class DispatchSerialization(unittest.TestCase):
    def test_lock_covers_entire_dispatch_and_close(self):
        for args in (['one'], ['one', '--close']):
            events = []
            with (patch.object(state, 'take_workspace', side_effect=lambda *a: events.append('lock') or True),
                  patch.object(state, 'release_merge_lock', side_effect=lambda *a: events.append('release')),
                  patch.object(dispatch, '_main', side_effect=lambda a: events.append('ledger read/start/save') or 0)):
                self.assertEqual(dispatch.main(args), 0)
            self.assertEqual(events, ['lock', 'ledger read/start/save', 'release'])

    def test_failed_dispatch_releases_only_owned_lock(self):
        for owned in (True, False):
            with (patch.object(state, 'take_workspace', return_value=owned),
                  patch.object(state, 'release_merge_lock') as release,
                  patch.object(dispatch, '_main', side_effect=ValueError('launch failed'))):
                with self.assertRaises(ValueError):
                    dispatch.main(['one'])
            self.assertEqual(release.call_count, int(owned))

    def test_read_only_dispatch_does_not_lock(self):
        for args in (['--help'], ['one', '--dry-run']):
            with patch.object(state, 'take_workspace') as take, patch.object(dispatch, '_main', return_value=0):
                self.assertEqual(dispatch.main(args), 0)
            take.assert_not_called()

    def test_real_process_lock_rejects_overlap_then_can_be_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            program = ('import sys; from pathlib import Path; from shiftboss.state import take_workspace, release_merge_lock; '
                       'p=Path(sys.argv[1]); take_workspace(p,"fixture child"); '
                       'print("owned",flush=True); sys.stdin.readline(); release_merge_lock(p)')
            child = subprocess.Popen([sys.executable, '-c', program, temp], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'owned')
                with patch.object(state, 'LOCK_WAIT_SECONDS', 0), redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(SystemExit, '被占用'):
                        state.take_workspace(repo, 'competing fixture')
                self.assertIn(f'pid={child.pid}', state.lock_path(repo).read_text())
                child.communicate('\n', timeout=10)
                self.assertEqual(child.returncode, 0)
                self.assertTrue(state.take_workspace(repo, 'next fixture'))
                self.assertFalse(state.take_workspace(repo, 'same process nested'))
                state.release_merge_lock(repo)
                self.assertFalse(state.lock_path(repo).exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_incomplete_owner_file_is_not_removed_as_dead(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            path = state.lock_path(repo)
            path.parent.mkdir()
            path.write_text('')
            with patch.object(state, 'LOCK_WAIT_SECONDS', 0):
                with self.assertRaises(SystemExit):
                    state.take_workspace(repo, 'contender')
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(), '')
