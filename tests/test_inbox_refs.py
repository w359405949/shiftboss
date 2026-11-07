import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shiftboss import inbox


class SurveyRefBatchTests(unittest.TestCase):
    def survey(self, refs, books, metadata, rejected=None, has_main=True):
        def git(git_dir, *args):
            self.assertEqual(args[0], 'for-each-ref')
            self.assertIn('--format=%(refname:short)%09%(committerdate:iso-strict)%09%(objectname)', args)
            self.assertEqual('--no-merged=main' in args, has_main)
            return refs

        with patch.object(inbox, '_git', side_effect=git) as calls, \
             patch.object(inbox, 'load_rejected', return_value=rejected or {}), \
             patch.object(inbox.subprocess, 'run', return_value=SimpleNamespace(returncode=0 if has_main else 1)), \
             patch('shiftboss.taskbook.Taskbook.find', side_effect=lambda repo, name: books.get(name)), \
             patch.object(inbox, 'taskbook_of', side_effect=lambda git_dir, branch, name: metadata.get((branch, name), (None, ''))):
            result = inbox.survey(Path('/repo'), Path('/bare'))
            self.assertEqual(calls.call_count, 1)
            return result

    def test_archived_and_rejected_refs_keep_original_filter(self):
        refs = 'codex/live\t2026-11-07T00:00:00Z\t' + 'a' * 40 + '\n' + \
               'codex/archived\t2026-11-07T00:00:00Z\t' + 'b' * 40 + '\n' + \
               'codex/rejected\t2026-11-07T00:00:00Z\t' + 'c' * 40
        meta = {'轮次': 2, '你的工作副本': '/copy', '交付模式': '自走'}
        result = self.survey(refs, {'live': SimpleNamespace(archived=False), 'archived': SimpleNamespace(archived=True)},
                             {('codex/live', 'live'): (meta, '执行状态：停下'), ('main', 'live'): (meta, '')},
                             {'rejected': ['c' * 40]})
        self.assertEqual([(r['name'], r['tip'], r['state'], r['mode']) for r in result],
                         [('live', 'a' * 40, '可核对', '自走')])

    def test_newer_main_round_is_still_progress(self):
        meta = {'轮次': 1}
        result = self.survey('codex/live\tdate\t' + 'd' * 40, {'live': SimpleNamespace(archived=False)},
                             {('codex/live', 'live'): (meta, '执行状态：停下'), ('main', 'live'): ({'轮次': 2}, '')})
        self.assertEqual(result[0]['state'], '进行中')

    def test_no_main_and_missing_task_remain_visible(self):
        result = self.survey('malformed\ncodex/missing\tdate\t' + 'e' * 40, {}, {}, has_main=False)
        self.assertEqual(result[0]['state'], '没有任务书')
        self.assertEqual(result[0]['tip'], 'e' * 40)


class DeliverySessionNoteTests(unittest.TestCase):
    def test_progress_delivery_does_not_hide_parked_session(self):
        row = {"name": "live", "signal": "进展"}
        with patch.object(inbox, 'session_state', return_value='停着'):
            note = inbox.delivery_session_note(row, {"session_id": "session"}, Path('/bare'))
        self.assertIn('会话停在等输入', note)
        self.assertIn('须续派或说明等待', note)

    def test_running_session_is_reported_without_resume_instruction(self):
        with patch.object(inbox, 'session_state', return_value='在干活'):
            note = inbox.delivery_session_note({"name": "live", "signal": "进展"}, {}, Path('/bare'))
        self.assertEqual(note, '会话：在干活')

    def test_closed_and_unstarted_sessions_do_not_trigger_resume(self):
        with patch.object(inbox, 'session_state') as state:
            self.assertEqual(inbox.delivery_session_note({"name": "live"}, {"closed": True}, Path('/bare')), '会话已关闭')
            self.assertEqual(inbox.delivery_session_note({"name": "live"}, None, Path('/bare')), '未派会话')
        state.assert_not_called()

    def test_stopped_delivery_keeps_ordinary_review_state(self):
        with patch.object(inbox, 'session_state', return_value='停着'):
            note = inbox.delivery_session_note({"name": "live", "signal": "停下"}, {}, Path('/bare'))
        self.assertEqual(note, '会话：停着')
