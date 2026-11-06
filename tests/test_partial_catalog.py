"""阶段进行中保留旧报告身份；完成报告不借此跳过校验。"""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shiftboss import regen
from shiftboss.outputs import catalog_document, catalog_files, merge_catalog


class PartialCatalog(unittest.TestCase):
    def setUp(self):
        self.stages = [dict(name=name, command=['node', name + '-new.mjs']) for name in ('a', 'b')]
        self.old = catalog_document([
            dict(stage=name, command=['python', name + '-old.py'], files=[name + '.json'])
            for name in ('a', 'b')
        ])
        self.done = [dict(stage='a', command=self.stages[0]['command'], files=['a.json'])]

    def test_pending_retains_its_real_command_until_executed(self):
        result = merge_catalog(self.old, self.stages, self.done, pending={'b'})
        self.assertEqual(result['producers'], [self.done[0], self.old['producers'][1]])
        with self.assertRaises(ValueError):
            catalog_files(result, [s['command'] for s in self.stages])
        complete = self.done + [dict(stage='b', command=self.stages[1]['command'], files=['b.json'])]
        self.assertEqual(merge_catalog(self.old, self.stages, complete)['producers'], complete)

    def test_final_or_unselected_stale_report_is_rejected(self):
        for pending in (None, set(), {'a'}):
            with self.assertRaises(ValueError):
                merge_catalog(self.old, self.stages, self.done, pending=pending)

    def test_pending_new_stage_can_be_absent_but_not_from_final(self):
        old = catalog_document([self.old['producers'][0]])
        self.assertEqual(merge_catalog(old, self.stages, self.done, pending={'b'})['producers'], self.done)
        with self.assertRaises(ValueError):
            merge_catalog(old, self.stages, self.done)

    def test_pending_does_not_exempt_fresh_wrong_command(self):
        wrong = self.done + [dict(stage='b', command=['python', 'wrong.py'], files=['b.json'])]
        with self.assertRaises(ValueError):
            merge_catalog(self.old, self.stages, wrong, pending={'b'})

    def test_old_pending_paths_still_have_exclusive_ownership(self):
        duplicate = [dict(stage='a', command=self.stages[0]['command'], files=['b.json'])]
        with self.assertRaisesRegex(ValueError, '同时归属'):
            merge_catalog(self.old, self.stages, duplicate, pending={'b'})

    def test_runner_publishes_pending_then_checks_final_commands(self):
        plan = {'stages': [{**s, 'inputs': ['source/']} for s in self.stages]}
        args = argparse.Namespace(full=False, stage=[], check=False, layer=[], all=False,
                                  steps=True, list=False, base=None)
        snapshots = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def run(label, commands, *, outputs, on_output, **kwargs):
                for stage in plan['stages']:
                    outputs.append(dict(stage=stage['name'], command=stage['command'], files=[stage['name']+'.json']))
                    on_output(outputs)
                    snapshots.append(json.loads((root/'catalog.json').read_text()))
                return True
            with (patch.object(regen, 'REPO', root), patch.object(regen, 'CATALOG', 'catalog.json'),
                  patch.object(regen, 'load_plan', return_value=plan),
                  patch.object(regen, 'stage_selection', return_value=plan['stages']),
                  patch.object(regen, 'previous_catalog', return_value=self.old),
                  patch.object(regen, 'run_all', side_effect=run)):
                self.assertEqual(regen._run(args), 0)
            self.assertEqual(snapshots[0]['producers'][1], self.old['producers'][1])
            final = json.loads((root/'catalog.json').read_text())
            self.assertEqual([p['command'] for p in final['producers']], [s['command'] for s in self.stages])


if __name__ == '__main__':
    unittest.main()
