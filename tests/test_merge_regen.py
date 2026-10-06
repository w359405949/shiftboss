"""合并时生成派生物：改了生成输入的合并在推送前就带上派生物，主副本不出现「新源码配旧派生物」。"""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

from shiftboss.outputs import CATALOG, catalog_document

PRODUCER = ("from pathlib import Path\nimport json\nfrom shiftboss.outputs import producer_main, declare_outputs\n"
            "@producer_main\ndef main():\n p=Path.cwd()/'asset.json'\n declare_outputs([p])\n"
            " p.write_text(json.dumps({'value':Path('value.txt').read_text()}))\n return 0\nraise SystemExit(main())\n")


class MergeRegenerates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / 'repo'
        self.repo.mkdir()
        shutil.copy2(Path(__file__).resolve().parent / 'fixtures' / 'shiftboss.toml', self.repo / 'shiftboss.toml')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Workflow acceptance')
        self.git('config', 'user.email', 'workflow@localhost')
        (self.repo / '.gitignore').write_text('out/\n__pycache__/\n')
        (self.repo / 'value.txt').write_text('good')
        (self.repo / 'asset.json').write_text(json.dumps({'value': 'good'}))
        (self.repo / 'producer.py').write_text(PRODUCER)
        steps = [[sys.executable, '-B', 'producer.py']]
        plan = {'steps': steps, 'gates': [[sys.executable, '-c', 'raise SystemExit(0)']],
                'gate_layers': {'publish': {'gates': [0], 'generate': True}}, 'always_layers': ['publish'],
                'path_layers': {'producer.py': ['publish'], 'value.txt': ['publish'], 'docs/': []}}
        (self.repo / 'regen.json').write_text(json.dumps(plan))
        (self.repo / CATALOG).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / CATALOG).write_text(json.dumps(catalog_document([{'command': steps[0], 'files': ['asset.json']}])))
        self.commit('baseline')
        subprocess.run(['git', 'init', '--bare', '-q', str(base / 'origin.git')], check=True)
        self.git('remote', 'add', 'origin', str(base / 'origin.git'))
        self.git('push', '-q', 'origin', 'main')
        self.git('switch', '-q', '-c', 'incoming')

    def git(self, *args):
        p = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True, encoding='utf-8')
        if p.returncode:
            raise AssertionError(p.stderr)
        return p.stdout.strip()

    def commit(self, message):
        self.git('add', '--all')
        self.git('commit', '-qm', message)

    def run_merge(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'shiftboss.merge', *args], cwd=self.repo,
                              capture_output=True, text=True, encoding='utf-8')

    def parents(self, rev='HEAD'):
        return self.git('show', '-s', '--format=%P', rev).split()

    def worktrees(self):
        return [line for line in self.git('worktree', 'list', '--porcelain').splitlines() if line.startswith('worktree ')]

    def test_input_change_brings_derived_artifacts_with_the_merge(self):
        """改了生成输入：合并后 HEAD 是建在合并提交上的生成提交，派生物已是新值。改回只合并不生成时失败。"""
        (self.repo / 'value.txt').write_text('better')
        self.commit('better value')
        self.git('switch', '-q', 'main')
        p = self.run_merge('incoming')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('派生物已随合并生成', p.stdout)
        self.assertTrue(self.git('log', '-1', '--format=%s').startswith('Generate artifacts for '))
        self.assertEqual(len(self.parents('HEAD^')), 2)
        self.assertEqual(json.loads((self.repo / 'asset.json').read_text()), {'value': 'better'})
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(len(self.worktrees()), 1)

    def test_merge_without_input_change_does_not_generate(self):
        """只改非生成输入：HEAD 就是合并提交，不跑生成。"""
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs/note.md').write_text('docs only')
        self.commit('docs only')
        self.git('switch', '-q', 'main')
        p = self.run_merge('incoming')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('本次合并未改生成输入', p.stdout)
        self.assertEqual(len(self.parents()), 2)
        self.assertEqual(json.loads((self.repo / 'asset.json').read_text()), {'value': 'good'})

    def test_generation_failure_keeps_merge_local_and_regen_retries(self):
        """生成失败：合并提交留在本地，报出重试与放弃的命令；修好后 `--regen` 补上生成提交。"""
        (self.repo / 'producer.py').write_text('raise SystemExit(3)\n')
        (self.repo / 'value.txt').write_text('better')
        self.commit('broken producer')
        self.git('switch', '-q', 'main')
        before = self.git('rev-parse', 'HEAD')
        p = self.run_merge('incoming')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(f'shiftboss merge --regen {before}', p.stdout + p.stderr)
        self.assertEqual(len(self.parents()), 2)
        self.assertEqual(json.loads((self.repo / 'asset.json').read_text()), {'value': 'good'})
        self.assertEqual(len(self.worktrees()), 1)
        (self.repo / 'producer.py').write_text(PRODUCER)
        self.commit('fix producer')
        retry = self.run_merge('--regen', before)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertTrue(self.git('log', '-1', '--format=%s').startswith('Generate artifacts for '))
        self.assertEqual(json.loads((self.repo / 'asset.json').read_text()), {'value': 'better'})
        self.assertEqual(self.git('status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
