"""A stale catalog permits explicit retirement, never accidental missing outputs."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import merge
from shiftboss.outputs import catalog_document


class RetiredGeneratedOutputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Generated output acceptance')
        self.git('config', 'user.email', 'outputs@localhost')
        self.output = self.repo / 'old.js'
        self.output.write_text('export const value = 1;\n')
        self.catalog = self.repo / merge.CATALOG
        self.catalog.parent.mkdir(parents=True, exist_ok=True)
        self.catalog.write_text(json.dumps(catalog_document([
            {'command': ['python', 'producer.py'], 'files': ['old.js']}
        ])))
        self.commit('published original output')
        self.root = patch.object(merge, 'REPO', self.repo)
        self.root.start()
        self.addCleanup(self.root.stop)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, text=True,
                              capture_output=True, check=True).stdout.strip()

    def commit(self, message):
        self.git('add', '--all')
        self.git('commit', '-qm', message)

    def test_staged_retirement_and_later_merges_do_not_require_generation(self):
        self.git('rm', '-q', 'old.js')
        (self.repo / 'new.ts').write_text('export const value = 1;\n')
        self.assertIn('old.js', merge.derived_files())
        self.commit('move generated source to TypeScript')
        self.assertIn('old.js', merge.derived_files())
        self.assertFalse(self.output.exists())
        self.assertEqual(json.loads(self.catalog.read_text())['producers'][0]['files'], ['old.js'])

    def test_unstaged_missing_output_is_rejected(self):
        self.output.unlink()
        with self.assertRaises(merge.MergeError):
            merge.derived_files()

    def test_never_tracked_missing_declaration_is_rejected(self):
        self.catalog.write_text(json.dumps(catalog_document([
            {'command': ['python', 'producer.py'], 'files': ['never-written.js']}
        ])))
        self.commit('invalid missing output declaration')
        with self.assertRaises(merge.MergeError):
            merge.derived_files()

    def test_deleted_output_replaced_by_directory_is_rejected(self):
        self.git('rm', '-q', 'old.js')
        self.output.mkdir()
        with self.assertRaises(merge.MergeError):
            merge.derived_files()

    def test_deleted_output_replaced_by_broken_symlink_is_rejected(self):
        self.git('rm', '-q', 'old.js')
        self.output.symlink_to('missing-target')
        with self.assertRaises(merge.MergeError):
            merge.derived_files()
