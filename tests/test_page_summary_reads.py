from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shiftboss import patrol


class PageSummaryReads(unittest.TestCase):
    def test_many_routes_share_reads_but_next_render_is_fresh(self):
        remote = Path('/hub.git')
        red = {f'/?view=route-{i}': {'first': '2026-11-01', 'commit': 'scan', 'log': 'unused'}
               for i in range(205)}
        claims = {route: {'taskbook': f'owner-{i % 3}', 'first': item['first']}
                  for i, (route, item) in enumerate(red.items())}
        state = {'red': red}
        def read(path):
            return state if path.name == 'state.json' else claims if path.name == 'claims.json' else {}
        with (patch.object(patrol, 'state_dir', return_value=Path('/pages')),
              patch('shiftboss.page_followups.store', return_value=Path('/pages/reviewed.json')),
              patch.object(patrol, 'read_state', side_effect=read),
              patch.object(patrol, 'lane_pid', return_value=False),
              patch.object(patrol, 'owner', return_value={'archived': True, 'merge': 'fixed'}) as owner,
              patch.object(patrol, 'git', return_value=SimpleNamespace(returncode=0)) as git,
              patch.object(patrol.verify, 'index_of', return_value={'last_consistent': 'published'}) as index):
            first = patrol.page_lines(remote)
            self.assertEqual(owner.call_count, 3)
            self.assertEqual(git.call_count, 3)
            self.assertEqual(index.call_count, 1)
            self.assertEqual(len([line for line in first if line.startswith('- 页面红 ')]), 3)
            for i in range(205):
                self.assertIn(f'route-{i}', '\n'.join(first))
            owner.return_value = {'archived': False, 'merge': None}
            second = patrol.page_lines(remote)
            self.assertEqual(owner.call_count, 6)
            self.assertTrue(all('｜在修｜' in line for line in second[1:]))

    def test_unowned_routes_are_all_kept_and_not_cached_across_calls(self):
        state = {'red': {'/?view=one': {'first': 'first', 'reasons': ['layout: bad'], 'log': 'unused'}}}
        with (patch.object(patrol, 'state_dir', return_value=Path('/pages')),
              patch.object(patrol, 'read_state', side_effect=lambda p: state if p.name == 'state.json' else {}),
              patch.object(patrol, 'lane_pid', return_value=False),
              patch.object(patrol, 'claim_rules', return_value={}) as rules):
            self.assertIn('- 页面红 1｜无人负责｜one', patrol.page_lines(Path('/hub.git')))
            patrol.page_lines(Path('/hub.git'))
            self.assertEqual(rules.call_count, 2)


if __name__ == '__main__':
    unittest.main()
