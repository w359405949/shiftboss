"""巡检优先级和发布反复前进时的组内覆盖。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shiftboss.patrol import Patrol, route_order


class PatrolOrder(unittest.TestCase):
    def test_priority_groups_keep_oldest_first(self):
        routes = ['normal', 'red-new', 'impact-new', 'red-old', 'impact-old', 'never']
        checked = {'normal': '0', 'red-new': '4', 'impact-new': '3',
                   'red-old': '2', 'impact-old': '1'}
        self.assertEqual(route_order(routes, ['impact-new', 'impact-old', 'absent'],
                                     ['red-new', 'red-old'], checked),
                         ['impact-old', 'impact-new', 'red-old', 'red-new', 'never', 'normal'])

    def test_overlaps_and_missing_routes_do_not_change_catalog(self):
        routes = ['first', 'second', 'third']
        affected = ['second', 'second', 'missing']
        reds = ['second', 'first', 'missing']
        checked = {}
        self.assertEqual(route_order(routes, affected, reds, checked),
                         ['second', 'first', 'third'])
        self.assertEqual(routes, ['first', 'second', 'third'])
        self.assertEqual(affected, ['second', 'second', 'missing'])
        self.assertEqual(reds, ['second', 'first', 'missing'])
        self.assertEqual(checked, {})

    def test_unchecked_ties_keep_declared_order(self):
        self.assertEqual(route_order(['a', 'b', 'c'], ['b', 'a'], [], {}),
                         ['b', 'a', 'c'])

    def test_real_watch_resumes_coverage_after_publication_changes(self):
        routes = ['hot-a', 'hot-b', 'cold-a', 'cold-b']
        checked = {'hot-a': '2026-11-05', 'hot-b': '2026-11-05',
                   'cold-a': '2026-11-01', 'cold-b': '2026-11-02'}
        seen = []
        with tempfile.TemporaryDirectory() as temporary:
            for release in range(2):
                directory = Path(temporary)
                worker = Patrol.__new__(Patrol)
                worker.directory = directory
                worker.remote = directory
                worker.workspace = directory
                worker.pass_release = f'new-{release}'
                worker.state = {'swept_release': f'old-{release}', 'pass': release,
                                'checked': checked, 'red': {'hot-a': {}, 'cold-b': {}}}
                worker.prepare = lambda: None
                worker.save = lambda: None
                worker.checkout_tip = lambda **_: worker.pass_release
                worker.policy = lambda: {'list': ['list'], 'check': ['check']}
                worker.routes = lambda *_, **__: list(routes)
                worker.yield_to_verify = lambda _: None
                batch = []
                worker.check = lambda *args: (0, directory / 'unused.log')

                def record(route, code, log, sha):
                    batch.append(route)
                    seen.append(route)
                    checked[route] = f'2026-11-06T00:00:{len(seen):02d}'
                    if len(batch) == 2:
                        (directory / 'stop').touch()

                worker.record = record
                with patch('shiftboss.patrol.verify.index_of',
                           return_value={'last_consistent': worker.pass_release}), \
                     patch('shiftboss.patrol.shutil.disk_usage',
                           return_value=SimpleNamespace(free=10 * 2**30)):
                    worker.watch()
                self.assertFalse((directory / 'worker.lock').exists())
                self.assertEqual(len(batch), 2)
                if release == 0:
                    self.assertEqual(batch, ['cold-a', 'cold-b'])
        self.assertEqual(seen, ['cold-a', 'cold-b', 'hot-a', 'hot-b'])


if __name__ == '__main__':
    unittest.main()
