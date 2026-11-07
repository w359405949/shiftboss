"""Reviewed page results stay red; only the identical reviewed result stops prompting."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import page_followups, patrol
from shiftboss.state import read_state, write_state


class PageFollowups(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'hub.git'
        self.route = '/?view=save'
        self.log = self.root / 'original.log'
        self.log.write_text('actual ready result; resource coverage incomplete\n')
        self.evidence = self.root / 'review.json'
        self.evidence.write_text('{"decision":"await exact missing consumer evidence"}\n')
        self.red = {'first': 'first', 'last': 'latest', 'commit': 'source', 'log': str(self.log),
                    'code': 2, 'reasons': ['resource incomplete']}
        patrol.state_dir(self.remote).mkdir(parents=True)
        write_state(patrol.state_dir(self.remote) / 'state.json', {'red': {self.route: self.red}})
        self.claim = {'first': 'first', 'taskbook': 'resource-audit'}
        self.addCleanup(patch.stopall)
        patch.object(patrol, 'owner', return_value=None).start()

    def acknowledge(self):
        return page_followups.record(self.remote, self.route, 'No new attributable product error', self.evidence)

    def test_same_result_stays_red_and_new_result_restores_reminder(self):
        before = read_state(patrol.state_dir(self.remote) / 'state.json')
        item = self.acknowledge()
        self.assertEqual(patrol.red_state(self.remote, self.red, self.claim, route=self.route), ('已审，待新证据', 'resource-audit'))
        self.assertNotEqual(patrol.red_state(self.remote, self.red, self.claim, route='/?view=other')[0], '已审，待新证据')
        self.assertEqual(read_state(patrol.state_dir(self.remote) / 'state.json'), before)
        self.assertEqual(item['reason'], 'No new attributable product error')
        for key, value in [('last', 'new completion'), ('first', 'new episode'), ('commit', 'new source'),
                           ('log', 'new original'), ('code', 1), ('reasons', ['new product error'])]:
            with self.subTest(key=key):
                changed = dict(self.red, **{key: value})
                self.assertFalse(page_followups.matches(item, changed))
                self.assertNotEqual(patrol.red_state(self.remote, changed, self.claim, route=self.route)[0], '已审，待新证据')

    def test_missing_changed_and_expired_evidence_restores_reminder(self):
        item = self.acknowledge()
        expired = deepcopy(item)
        expired['expires'] = 0
        self.assertFalse(page_followups.matches(expired, self.red))
        self.evidence.write_text('changed review')
        self.assertFalse(page_followups.matches(item, self.red))
        item = self.acknowledge()
        self.log.write_text('new error in the original')
        self.assertFalse(page_followups.matches(item, self.red))
        item = self.acknowledge()
        self.log.unlink()
        self.assertFalse(page_followups.matches(item, self.red))
        self.assertEqual(patrol.red_state(self.remote, self.red, self.claim, route=self.route)[0], '任务书缺失')

    def test_declining_to_acknowledge_has_no_side_effects(self):
        with self.assertRaisesRegex(ValueError, 'reason'):
            page_followups.record(self.remote, self.route, ' ', self.evidence)
        with self.assertRaisesRegex(ValueError, 'complete'):
            page_followups.record(self.remote, '/?view=absent', 'missing provider', self.evidence)
        with self.assertRaises(FileNotFoundError):
            page_followups.record(self.remote, self.route, 'missing provider', self.root / 'missing.json')
        self.assertFalse(page_followups.store(self.remote).exists())

    def test_new_result_during_recording_is_not_acknowledged(self):
        original = page_followups.file_identity
        def changed(path):
            if path == self.log:
                write_state(patrol.state_dir(self.remote) / 'state.json',
                            {'red': {self.route: dict(self.red, commit='new-source')}})
            return original(path)
        with patch.object(page_followups, 'file_identity', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'new result'):
                self.acknowledge()
        self.assertFalse(page_followups.store(self.remote).exists())

    def test_brief_keeps_reviewed_red_visible_and_still_prompts_for_other_pages(self):
        from shiftboss.brief import actionable
        self.acknowledge()
        write_state(patrol.state_dir(self.remote) / 'claims.json', {self.route: self.claim})
        with patch.object(patrol, 'lane_pid', return_value=None):
            lines = patrol.page_lines(self.remote)
            self.assertTrue(any('已审，待新证据' in line and 'save' in line for line in lines))
            self.assertFalse(any('页面红无主' in line for line in actionable([], [], [], [], [], [], lines)))
            write_state(patrol.state_dir(self.remote) / 'state.json',
                        {'red': {self.route: self.red, '/?view=other': self.red}})
            lines = patrol.page_lines(self.remote)
        self.assertTrue(any('页面红无主或修复无效 1 组' in line
                            for line in actionable([], [], [], [], [], [], lines)))
