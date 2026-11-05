import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shiftboss.mistakes import reconcile


class MistakesReconciliation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = self.root / 'control'
        self.control.mkdir()
        self.original = '# 错题本\n\n## 一\n\n原文甲\n\n## 二\n\n原文乙\n'.encode()
        (self.control / 'mistakes.md').write_bytes(self.original)
        (self.root / 'rule.md').write_text('existing rule')
        self.plan = {'source_sha256': hashlib.sha256(self.original).hexdigest(), 'entry_count': 2,
                     'groups': [{'key': 'one', 'ids': [1, 2], 'action': 'existing',
                                 'basis': ['rule.md#rule'], 'decision': 'covered'}]}
        self.path = self.root / 'plan.json'

    def run_plan(self, apply=False):
        self.path.write_text(json.dumps(self.plan))
        return reconcile(self.control, self.root, self.path, apply)

    def test_preview_cannot_modify_or_archive(self):
        self.assertEqual(self.run_plan()['resolved_entries'], 2)
        self.assertEqual((self.control / 'mistakes.md').read_bytes(), self.original)
        self.assertFalse((self.control / 'mistakes-archive').exists())

    def test_apply_preserves_exact_original_and_plan(self):
        result = self.run_plan(True)
        archive = Path(result['archive'])
        self.assertEqual((archive / 'original.md').read_bytes(), self.original)
        self.assertEqual((archive / 'plan.json').read_bytes(), self.path.read_bytes())
        self.assertNotIn('## ', (self.control / 'mistakes.md').read_text())
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.run_plan(True)

    def test_missing_duplicate_and_out_of_range_ids_are_rejected(self):
        for ids in ([1], [1, 1], [1, 3], [True, 2]):
            with self.subTest(ids=ids):
                self.plan['groups'][0]['ids'] = ids
                with self.assertRaises(ValueError):
                    self.run_plan(True)
                self.assertEqual((self.control / 'mistakes.md').read_bytes(), self.original)

    def test_stale_source_is_rejected_without_archive(self):
        (self.control / 'mistakes.md').write_bytes(self.original + b'new fact')
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.run_plan(True)
        self.assertFalse((self.control / 'mistakes-archive').exists())

    def test_missing_rule_is_not_a_completion_claim(self):
        self.plan['groups'][0]['basis'] = ['missing.py']
        with self.assertRaisesRegex(ValueError, '依据不存在'):
            self.run_plan(True)
        self.assertEqual((self.control / 'mistakes.md').read_bytes(), self.original)

    def test_pending_requires_owner_and_condition_and_stays_active(self):
        self.plan['groups'][0]['action'] = 'pending'
        with self.assertRaisesRegex(ValueError, '负责人'):
            self.run_plan(True)
        self.plan['pending'] = {'one': {'owner': 'task-one', 'completion': 'finite check passes'}}
        result = self.run_plan(True)
        self.assertEqual((result['resolved_entries'], result['pending_groups']), (0, 1))
        active = (self.control / 'mistakes.md').read_text()
        self.assertIn('task-one', active)
        self.assertIn('finite check passes', active)
        self.assertIn('1, 2', active)

    def test_existing_different_archive_is_never_overwritten(self):
        archive = self.control / 'mistakes-archive' / ('reconciliation-' + self.plan['source_sha256'][:16])
        archive.mkdir(parents=True)
        (archive / 'original.md').write_bytes(b'other content')
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            self.run_plan(True)
        self.assertEqual((self.control / 'mistakes.md').read_bytes(), self.original)
