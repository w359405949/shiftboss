from pathlib import Path
import subprocess
import tempfile
import unittest

from shiftboss import delivery_progress as progress


class DeliveryProgress(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.commit('src/app.ts', 'export const value = 1;\n')
        self.git_dir = self.root / '.git'

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()

    def commit(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git('add', '--', path)
        self.git('commit', '-qm', path)

    def report(self, task, text='waiting\n'):
        self.commit(f'shiftboss/plans/{task}/delivery.md', text)

    def count(self):
        return progress.snapshot(self.git_dir)['report_only_intakes']

    def test_report_updates_count_but_dispatch_and_repeat_reads_do_not(self):
        self.report('one')
        self.report('one', 'new result\n')
        self.commit('shiftboss/plans/one/rounds.md', 'next round\n')
        self.commit('docs/progress.md', 'explanation\n')
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.count(), 2)

    def test_source_integration_resets_and_new_report_starts_next_streak(self):
        self.report('one')
        self.report('two')
        self.commit('src/app.ts', 'export const value = 2;\n')
        self.assertEqual(self.count(), 0)
        self.report('three')
        self.assertEqual(self.count(), 1)

    def test_first_parent_merge_counts_only_the_delivery_that_landed(self):
        self.git('switch', '-qc', 'worker')
        self.report('one')
        self.report('one', 'final\n')
        self.git('switch', '-q', 'main')
        self.assertEqual(self.count(), 0)
        self.git('merge', '-q', '--no-ff', 'worker', '-m', 'accept')
        self.assertEqual(self.count(), 1)
        self.git('switch', '-qc', 'source-worker')
        self.report('two')
        self.commit('src/app.ts', 'export const value = 3;\n')
        self.git('switch', '-q', 'main')
        self.git('merge', '-q', '--no-ff', 'source-worker', '-m', 'source intake')
        self.assertEqual(self.count(), 0)

    def test_batch_counts_each_task_and_archive_does_not_count(self):
        for task in ('one', 'two'):
            p = self.root / f'shiftboss/plans/{task}/delivery.md'
            p.parent.mkdir(parents=True)
            p.write_text('done\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'batch')
        self.assertEqual(self.count(), 2)
        archive = self.root / 'shiftboss/plans/archive'
        archive.mkdir()
        self.git('mv', 'shiftboss/plans/one', 'shiftboss/plans/archive/one')
        self.git('commit', '-qm', 'archive')
        self.assertEqual(self.count(), 2)

    def test_warning_is_thresholded_and_source_change_clears_it(self):
        self.commit('shiftboss.toml', '[brief]\nreport_only_warning_after = 2\n')
        self.report('one')
        self.assertNotIn('⚠', '\n'.join(progress.summary(self.root, self.git_dir)))
        self.report('two')
        self.assertIn('⚠', '\n'.join(progress.summary(self.root, self.git_dir)))
        self.commit('src/app.ts', 'export const value = 4;\n')
        self.assertNotIn('⚠', '\n'.join(progress.summary(self.root, self.git_dir)))
        self.commit('shiftboss.toml', '[automation]\ndelivery_progress = false\n')
        self.assertEqual(progress.summary(self.root, self.git_dir), ['【交付推进：关】'])

    def test_task_evidence_scripts_do_not_masquerade_as_product_code(self):
        self.report('one')
        self.commit('shiftboss/plans/one/evidence/helper.py', 'print(1)\n')
        self.commit('out/helper.ts', 'export {};\n')
        self.assertEqual(self.count(), 1)


if __name__ == '__main__':
    unittest.main()
