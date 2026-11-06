"""会话路由不读取正在解决冲突的执行副本配置。"""
import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from shiftboss.workers import codex


class SessionDiscovery(unittest.TestCase):
    def test_conflicting_copy_configuration_does_not_hide_started_session(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            copy = base / 'copy'
            copy.mkdir()
            (copy / 'shiftboss.toml').write_text('<<<<<<< ours\ninvalid TOML\n=======\n>>>>>>> main\n')
            home = base / 'shared-home'
            (home / 'sessions').mkdir(parents=True)
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            def rollout(name, timestamp):
                (home / 'sessions' / f'rollout-{name}.jsonl').write_text(json.dumps({
                    'payload': {'session_id': name, 'timestamp': timestamp,
                                'cwd': str(copy), 'originator': 'codex-tui'}}) + '\n')
            rollout('old', '2000-01-01T00:00:00Z')
            rollout('started', stamp)
            def canonical_home(root=None):
                if root is not None:
                    raise ValueError('executor configuration is mid-merge')
                return home
            with patch.object(codex, 'worker_home', side_effect=canonical_home) as located:
                self.assertEqual(codex.newest_session(str(copy), time.time()), 'started')
                located.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
