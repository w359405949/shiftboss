"""Real Git acceptance for the merge and dispatch publication paths."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import io
import psutil

from shiftboss.outputs import CATALOG, catalog_document
from shiftboss import dispatch
from shiftboss.config import control_dir
from shiftboss.workers import codex as codex_worker

def taskbook_at(path):
    """从一个单文件任务书路径造 `Taskbook`。

    **`publish_note` 收的是 `Taskbook` 不是路径**——布局归 `shiftboss.taskbook` 管，
    测试也走同一个入口，不另造一条只有测试用的路径分支。
    """
    from shiftboss.taskbook import Taskbook
    return Taskbook(path.stem, path, is_dir=False, archived=False)


class TaskbookLayoutOwnership(unittest.TestCase):
    """任务书是单文件还是目录，只许 `shiftboss.taskbook` 一个模块知道。

    **这条不是洁癖。** `inbox status` 曾经按 `shiftboss/plans/*.md` 自己列目录，
    于是目录布局的线一条都不显示：表是空的、末尾还报「共 0 份在办任务书」，
    而那张表是「现在什么在跑」的唯一入口。
    **散在外面的布局知识不会报错，它只是安静地少给你几行**——
    少给的那几行恰好是新格式的线，也就是正在干活的那些。
    """

    def test_no_module_enumerates_docs_plans_itself(self):
        offenders = []
        for module in sorted((Path(__file__).parents[1] / 'src' / 'shiftboss').rglob('*.py')):
            if module.name in ('taskbook.py',) or module.name.startswith('test_'):
                continue
            for number, line in enumerate(module.read_text(encoding='utf-8').splitlines(), 1):
                if 'plans' not in line:
                    continue
                # 自己列目录，或者自己拼 `<名字>.md`——布局知识只有这两种露头方式。
                # 写死的固定路径（测试夹具里那种）不算：它不随布局变。
                if '.glob(' in line or '.iterdir(' in line or ('.md' in line and '{' in line):
                    offenders.append(f'{module.name}:{number}: {line.strip()}')
        self.assertEqual(offenders, [], '自己列 shiftboss/plans 的地方（改走 shiftboss.taskbook）：'
                                        + chr(10).join(offenders))


class TaskbookKinds(unittest.TestCase):
    """各子类型的骨架与读表头的代码必须对得上；生命周期一律自走（README 2.4）。"""

    def test_each_kind_builds_a_consistent_header(self):
        from shiftboss.taskbook import KINDS, Taskbook, new
        self.assertTrue({'task', 'repair', 'conflict'} <= set(KINDS))
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            for kind in KINDS:
                name = f'probe-{kind}'
                new(repo, name, kind=kind)
                book = Taskbook.find(repo, name)
                meta, contract = book.meta(), book.contract_text()
                self.assertEqual(meta['子类型'], kind)
                self.assertIn(name, contract)
                self.assertNotIn('{name}', contract)
                self.assertIn('### 派发条件', contract, kind)
                self.assertIn('### 交付条件', contract, kind)
                self.assertTrue(dispatch.keeps_going(meta['交付模式']), kind)
                self.assertIn('2.4', contract, kind)
            with self.assertRaises(ValueError):
                new(repo, 'probe-bogus', kind='bogus')

    def test_copy_is_given_by_name_not_path(self):
        from shiftboss.taskbook import copy_path
        for bad in (r'C:\scratch\boss-mode.md', 'E:/x/codex6', 'boss-mode.md'):
            with self.assertRaises(ValueError):
                copy_path(Path('.'), bad)


class FastMergeAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        shutil.copy2(Path(__file__).resolve().parent/'fixtures'/'shiftboss.toml', self.repo/'shiftboss.toml')
        self.git('init','-q','-b','main')
        self.git('config','user.name','Workflow acceptance')
        self.git('config','user.email','workflow@localhost')
        commands = [[sys.executable, '-c', "from pathlib import Path; Path('unexpected-generation').touch()"]]
        (self.repo/'regen.json').write_text(json.dumps({'steps':commands,'gates':[[sys.executable,'-c','raise SystemExit(1)']]}))
        for name in ('one.json','two.json'):
            (self.repo/name).write_text('{}')
        catalog = catalog_document([{'command':commands[0], 'files':['one.json','two.json']}])
        (self.repo/CATALOG).parent.mkdir(parents=True,exist_ok=True)
        (self.repo/CATALOG).write_text(json.dumps(catalog))
        self.commit('baseline')
        self.git('switch','-q','-c','incoming')
    def git(self,*args):
        p=subprocess.run(['git','-C',str(self.repo),*args],capture_output=True,text=True,encoding='utf-8')
        if p.returncode: raise AssertionError(p.stderr)
        return p.stdout.strip()
    def commit(self,message):
        self.git('add','--all'); self.git('commit','-qm',message)
    def merge(self):
        return subprocess.run([sys.executable,'-B','-m','shiftboss.merge','incoming'],cwd=self.repo,capture_output=True,text=True,encoding='utf-8')
    def test_clean_merge_does_not_generate_or_test(self):
        (self.repo/'note.md').write_text('incoming')
        self.commit('incoming'); self.git('switch','-q','main')
        p=self.merge(); self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertFalse((self.repo/'unexpected-generation').exists())
        self.assertEqual(len(self.git('show','-s','--format=%P').split()),2)
        self.assertEqual(self.git('status','--porcelain'),'')
    def test_merge_shows_removed_assertions(self):
        """删掉的断言要出现在**合并这一步**的输出里。

        「不许放宽或删除既有断言」是每份合同都写的一条，而它唯一被发现的时刻是核对。
        核对时另开一条 grep 去数是不够的：**那条 grep 串在合并前面时退 0，合并照跑**。
        拦不住下一步的检查等于没有检查，所以它长在合并里，和合并同一份输出。
        它不拦合并——重写、换等值判法、换更强的检查都会删行，判归属要人读交回。
        """
        tests = self.repo/'engine/tests'
        tests.mkdir(parents=True)
        (tests/'test_thing.py').write_text('def check():\n    assert 1 == 1, "原来的判据"\n')
        self.commit('a test with one assertion')
        self.git('switch','-q','main'); self.git('merge','-q','incoming'); self.git('switch','-q','incoming')
        (tests/'test_thing.py').write_text('def check():\n    pass\n')
        self.commit('drop the assertion'); self.git('switch','-q','main')
        p=self.merge()
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)          # 不拦
        self.assertIn('快速检查 测试断言: +0 -1',p.stdout)
        self.assertIn('原来的判据',p.stdout)                         # 删掉的那行摆出来

    def _rewrite_assertion(self, before, after):
        tests = self.repo/'engine/tests'
        tests.mkdir(parents=True)
        (tests/'test_thing.py').write_text(f'def check(x):\n    assert x in {before}\n')
        self.commit('a test with one assertion')
        self.git('switch','-q','main'); self.git('merge','-q','incoming'); self.git('switch','-q','incoming')
        (tests/'test_thing.py').write_text(f'def check(x):\n    assert x in {after}\n')
        self.commit('rewrite the assertion'); self.git('switch','-q','main')
        p=self.merge()
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        return p.stdout

    def test_merge_does_not_count_extended_assertion_as_removed(self):
        """只增不减的改写不计为删除。"""
        out=self._rewrite_assertion('["a", "b"]', '["a", "b", "c"]')
        self.assertIn('快速检查 测试断言: +1 -0',out)
        self.assertIn('只增不减的改写 1 行',out)

    def test_merge_counts_narrowed_assertion_as_removed(self):
        """丢了记号的改写仍计为删除。"""
        out=self._rewrite_assertion('["a", "b"]', '["a"]')
        self.assertIn('快速检查 测试断言: +1 -1',out)

    def test_bad_json_stops_before_commit(self):
        (self.repo/'bad.json').write_text('{')
        self.commit('invalid json'); self.git('switch','-q','main')
        before=self.git('rev-parse','HEAD'); p=self.merge()
        self.assertNotEqual(p.returncode,0); self.assertEqual(self.git('rev-parse','HEAD'),before)
        self.assertTrue((self.repo/'.git/MERGE_HEAD').exists())
    def test_derived_conflict_takes_whole_current_group(self):
        (self.repo/'one.json').write_text('{"incoming":1}')
        (self.repo/'two.json').write_text('{"incoming":2}')
        self.commit('incoming'); self.git('switch','-q','main')
        (self.repo/'one.json').write_text('{"current":1}')
        self.commit('current'); p=self.merge()
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertEqual((self.repo/'two.json').read_text(),'{}')
        self.assertEqual(json.loads((self.repo/'one.json').read_text()),{'current':1})
    def test_source_conflict_is_not_resolved(self):
        (self.repo/'note.md').write_text('incoming');self.commit('incoming')
        self.git('switch','-q','main'); (self.repo/'note.md').write_text('current');self.commit('current')
        p=self.merge(); self.assertNotEqual(p.returncode,0)
        self.assertIn('note.md',self.git('diff','--name-only','--diff-filter=U'))
    def test_existing_lock_is_retained(self):
        self.git('switch','-q','main'); lock=self.repo/'.git/shiftboss-merge-in-flight';lock.write_text('owner')
        p=self.merge(); self.assertNotEqual(p.returncode,0);self.assertEqual(lock.read_text(),'owner')
    def test_dispatch_rejects_unpushed_commit(self):
        self.git('switch','-q','main')
        remote=Path(self.temp.name)/'origin.git'
        subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        self.git('remote','add','origin',str(remote)); self.git('push','-q','origin','main')
        path=self.repo/'task.md';path.write_text('## 4. 轮次记录\n');self.commit('unrelated unpushed')
        before=self.git('rev-parse','HEAD'); remote_before=subprocess.check_output(['git',f'--git-dir={remote}','rev-parse','main'],text=True).strip()
        original=dispatch.REPO;dispatch.REPO=self.repo
        try:
            with self.assertRaisesRegex(ValueError,'unrelated unpushed') as error:
                dispatch.publish_note(taskbook_at(path),'probe')
            print(str(error.exception))
        finally: dispatch.REPO=original
        self.assertEqual(self.git('rev-parse','HEAD'),before)
        self.assertNotEqual(self.git('rev-parse','origin/main'),before)
        self.assertEqual(subprocess.check_output(['git',f'--git-dir={remote}','rev-parse','main'],text=True).strip(),remote_before)
        self.assertEqual(path.read_text(),'## 4. 轮次记录\n')
    def test_dispatch_publishes_only_note(self):
        self.git('switch','-q','main')
        remote=Path(self.temp.name)/'origin.git';subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        path=self.repo/'task.md';path.write_text('## 4. 轮次记录\n');self.commit('task')
        self.git('remote','add','origin',str(remote)); self.git('push','-q','origin','main')
        original=dispatch.REPO;dispatch.REPO=self.repo
        try: dispatch.publish_note(taskbook_at(path),'probe')
        finally: dispatch.REPO=original
        self.assertEqual(self.git('rev-parse','HEAD'),self.git('rev-parse','origin/main'))
        self.assertEqual(self.git('diff','--name-only','HEAD^','HEAD'),'task.md')

    def lifecycle_merge(self, mode, invalid=False, preclosed=False, done=False):
        self.git('branch', '-m', 'codex/probe')
        remote = Path(self.temp.name) / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        plans = self.repo / 'shiftboss/plans'; plans.mkdir(parents=True)
        (plans/'probe.md').write_text(
            f'| 状态 | `待派` |\n| 交付模式 | `{mode}` |\n| 分支 | `codex/probe` |\n'
            f'| 你的工作副本 | `{self.repo}` |\n', encoding='utf-8')
        (self.repo/'payload.json').write_text('{' if invalid else '{}')
        self.commit('delivery'); self.git('push', '-q', 'origin', 'codex/probe')
        ledger = {'probe': {'session_id':'preserved-id', 'copy':str(self.repo),
                            'branch':'codex/probe', 'closed':preclosed}}
        state = control_dir(remote)/'sessions.json'
        state.write_text(json.dumps(ledger), encoding='utf-8')
        self.git('switch', '-q', 'main')
        run = lambda *args: subprocess.run([sys.executable, '-B', '-m', 'shiftboss.merge', *args],
                                           cwd=self.repo, capture_output=True, text=True,
                                           encoding='utf-8')
        if done:
            # 分支还没合进来时 --done 必须拒，免得把交付丢在分支上。
            early = run('--done', 'probe')
            self.assertNotEqual(early.returncode, 0, early.stdout + early.stderr)
        result = run('origin/codex/probe')
        print(result.stdout)
        after = json.loads(state.read_text(encoding='utf-8'))
        self.assertEqual(after['probe']['session_id'], 'preserved-id')
        archived = plans/'archive'/'probe.md'
        if invalid:
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(after['probe'].get('closed'))
            return
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 合并不关会话、不归档：做没做完由派活方核对后 --done 定（README 2.4）。
        self.assertEqual(bool(after['probe'].get('closed')), preclosed)
        self.assertFalse(archived.exists(), result.stdout)
        self.assertIn('--done probe', result.stdout)
        if done:
            finished = run('--done', 'probe')
            self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
            after = json.loads(state.read_text(encoding='utf-8'))
            self.assertTrue(after['probe'].get('closed'))
            self.assertTrue(archived.exists(), finished.stdout)
            self.assertFalse((plans/'probe.md').exists(), finished.stdout)
            self.assertIn('`已归档`', archived.read_text(encoding='utf-8'))

    def test_merge_leaves_session_and_taskbook(self):
        self.lifecycle_merge('单轮')

    def test_merge_leaves_self_driving_line(self):
        self.lifecycle_merge('自走')

    def test_failed_merge_keeps_session(self):
        self.lifecycle_merge('自走', invalid=True)

    def test_done_closes_and_archives_after_merge(self):
        self.lifecycle_merge('自走', done=True)

    def test_done_archives_even_when_session_already_closed(self):
        self.lifecycle_merge('自走', preclosed=True, done=True)

    def test_inbox_without_main_reports_unknown(self):
        remote = Path(self.temp.name)/'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        plans = self.repo/'shiftboss/plans'; plans.mkdir(parents=True)
        (plans/'probe.md').write_text(
            '| 状态 | `待派` |\n| 分支 | `codex/probe` |\n', encoding='utf-8')
        self.commit('task without integration branch')
        self.git('push', '-q', 'origin', 'HEAD:codex/probe')
        for command in ('list', 'status'):
            result = subprocess.run([sys.executable, '-B', '-m', 'shiftboss.inbox', command],
                                    cwd=self.repo, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('## ⚠ 还没做完、但现在没人在做：算不出', result.stdout)
            self.assertIn('origin 尚无 refs/heads/main', result.stdout)
            self.assertIn('probe', result.stdout)  # Rest of the inbox still renders.
            self.assertIn('进行中' if command == 'list' else '共 1 份在办任务书', result.stdout)
        # A non-repository is a genuine read failure, not the missing-ref case.
        from shiftboss import inbox
        invalid = Path(self.temp.name)/'not-a-repository'; invalid.mkdir()
        with self.assertRaises(subprocess.CalledProcessError):
            inbox.unattended_warning(self.repo, invalid, {})

    def test_inbox_unattended_warning(self):
        self.git('switch', '-q', 'main')
        remote = Path(self.temp.name)/'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        plans = self.repo/'shiftboss/plans'; plans.mkdir(parents=True)

        def task(name, status):
            (plans/f'{name}.md').write_text(
                f'| 状态 | `{status}` |\n| 分支 | `codex/{name}` |\n'
                f'| 你的工作副本 | `{self.repo}` |\n', encoding='utf-8')

        for name, status in [('unfinished', '在办'), ('deliberately-idle', '低频'),
                             ('pushed', '在办')]:
            task(name, status)
        self.commit('taskbooks'); self.git('push', '-q', 'origin', 'main')
        self.git('push', '-q', 'origin', 'main:codex/unfinished', 'main:codex/deliberately-idle')
        self.git('switch', '-q', '-c', 'codex/pushed')
        (self.repo/'delivery.txt').write_text('unmerged push')
        self.commit('unmerged delivery'); self.git('push', '-q', 'origin', 'codex/pushed')
        pushed = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        ledger = {name: {'closed':True} for name in ('unfinished', 'deliberately-idle', 'pushed')}
        ledger['running'] = {'closed':False}
        state = control_dir(remote)/'sessions.json'
        quota = control_dir(remote)/'quota.json'
        quota.write_text('{"codex":3}', encoding='utf-8')

        def report(command):
            state.write_text(json.dumps(ledger), encoding='utf-8')
            result = subprocess.run([sys.executable, '-B', '-m', 'shiftboss.inbox', command],
                                    cwd=self.repo, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            marker = '## ⚠ 还没做完、但现在没人在做的 '
            if marker not in result.stdout:
                return ''
            return result.stdout.split(marker, 1)[1].split('\n\n', 3)[0:3]

        def section(command):
            return '\n\n'.join(report(command))

        for command in ('list', 'status'):
            warning = section(command)
            self.assertIn('`unfinished`', warning)
            self.assertNotIn('`deliberately-idle`', warning)
            self.assertNotIn('`pushed`', warning)
            self.assertIn('在跑 1/3 条，空位 2 条', warning)
        task('unfinished', '已归档')
        self.assertEqual(section('list'), '')
        self.assertEqual(section('status'), '')
        # No ledger entry is also unattended; an open entry is not.
        task('unfinished', '待派'); del ledger['unfinished']
        self.assertIn('`unfinished`', section('list'))
        ledger['unfinished'] = {}
        self.assertEqual(section('list'), '')
        ledger['unfinished'] = {'closed':True}
        quota.write_text('{"codex":0}', encoding='utf-8')
        self.assertIn('未分配额度，在跑 1 条', section('status'))
        # A rejected push disappears from survey, but remains unmerged.
        (control_dir(remote)/'inbox-rejected.json').write_text(json.dumps({'pushed':[pushed]}), encoding='utf-8')
        self.assertNotIn('`pushed`', section('list'))


class SessionReuseAcceptance(unittest.TestCase):
    """收摊过的会话不许再被 resume：那是噪音和额度，不是上下文。"""

    def test_closed_line_starts_a_fresh_session(self):
        live = {"session_id": "s1"}
        self.assertEqual(dispatch.start_fresh(live, forced=False, burned=False)[0], False)
        retired = {"session_id": "s1", "closed": True}
        fresh, why = dispatch.start_fresh(retired, forced=False, burned=False)
        self.assertTrue(fresh, "--close / 合并自动关之后再派，必须开新会话")
        self.assertIn("收摊", why)
        self.assertTrue(dispatch.start_fresh(live, forced=True, burned=False)[0])
        self.assertTrue(dispatch.start_fresh(live, forced=False, burned=True)[0])
        self.assertTrue(dispatch.start_fresh({}, forced=False, burned=False)[0])


class RoundNumbering(unittest.TestCase):
    def test_fully_compressed_rounds_continue_numbering(self):
        """轮次标题全部压掉后，下一轮编号接在「已压掉…第 X–Y 轮」的 Y 之后。"""
        from shiftboss.taskbook import Taskbook, new
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            new(root, 'probe', kind='task')
            book = Taskbook.find(root, 'probe')
            (book.path/'rounds.md').write_text(
                '# 轮次记录\n\n## 当前盘面\n\n1. x\n\n## 已压掉的 35 轮记录（第 1–35 轮，见 git）\n',
                encoding='utf-8')
            self.assertEqual(dispatch.round_number(Taskbook.find(root, 'probe')), 36)


class ForeignSessionAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.copy = self.root/'copy with spaces'; self.copy.mkdir()
        self.other = self.root/'other'; self.other.mkdir()

    def start(self, directory):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                   cwd=directory,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait()
        self.addCleanup(cleanup)
        return process

    def test_live_cwd_without_c_flag(self):
        process = self.start(self.copy)
        with patch.object(dispatch, 'executor_processes', return_value=[psutil.Process(process.pid)]):
            self.assertTrue(dispatch.tui_alive(str(self.copy)))
            self.assertFalse(dispatch.tui_alive(str(self.other)))
        other = self.start(self.other)
        with patch.object(dispatch, 'executor_processes', return_value=[psutil.Process(other.pid)]):
            self.assertFalse(dispatch.tui_alive(str(self.copy)))

    def test_unknown_process_is_not_absent(self):
        process = unittest.mock.Mock(pid=123)
        process.cmdline.side_effect = psutil.AccessDenied(123)
        with patch.object(dispatch, 'executor_processes', return_value=[process]):
            with self.assertRaisesRegex(RuntimeError, '拒绝当作不在'):
                dispatch.tui_alive(str(self.copy))

    def test_configured_argv_requires_launch_identity(self):
        process = unittest.mock.Mock(pid=123)
        process.create_time.return_value = 456.0
        process.name.return_value = 'codex.exe'
        process.cmdline.return_value = ['codex.exe', '-C', str(self.copy), '-s',
            'danger-full-access', '-a', 'never', '-c', 'projects={trust_level="trusted"}']
        entry = {'session_id':'probe-id', 'copy':str(self.copy)}
        with patch.object(dispatch, 'copy_processes', return_value=[process]):
            self.assertIsNotNone(dispatch.foreign_session_reason(str(self.copy), entry))
            entry['processes'] = [{'pid':123, 'created':456.0}]
            self.assertIsNone(dispatch.foreign_session_reason(str(self.copy), entry))
            entry['processes'][0]['created'] = 455.0
            self.assertIsNotNone(dispatch.foreign_session_reason(str(self.copy), entry))

    def test_recover_and_dispatch_refuse_foreign_then_close(self):
        plans = self.root/'shiftboss/plans'; plans.mkdir(parents=True)
        (plans/'probe.md').write_text(
            f'| 状态 | `待派` |\n| 你的工作副本 | `{self.copy}` |\n'
            '| 分支 | `codex/probe` |\n| 完成标准 | `原型` |\n', encoding='utf-8')
        ledger = {'probe': {'session_id':'probe-id', 'copy':str(self.copy)}}
        process = self.start(self.copy)
        with patch.object(dispatch, 'REPO', self.root), \
             patch.object(dispatch, 'load_sessions', return_value=ledger), \
             patch.object(dispatch, 'save_sessions') as save, \
             patch.object(codex_worker, 'rollout_of', return_value=None), \
             patch.object(dispatch, 'executor_processes', side_effect=lambda:
                          [psutil.Process(process.pid)] if process.poll() is None else []), \
             patch.object(dispatch, 'launch_tui') as launch:
            for flags in (['--recover', '--dry-run'], ['--dry-run']):
                output = io.StringIO()
                with redirect_stdout(output):
                    result = dispatch.main(['probe', *flags])
                self.assertEqual(result, 2, output.getvalue())
                self.assertIn('关掉那个标签页', output.getvalue())
            self.assertEqual(dispatch.main(['probe', '--close']), 0)
            process.wait(timeout=10)
            self.assertTrue(ledger['probe']['closed'])
            self.assertEqual(ledger['probe']['session_id'], 'probe-id')
            self.assertEqual(dispatch.main(['probe', '--recover', '--dry-run']), 0)
            launch.assert_not_called()
            save.assert_called_once()

    def test_prompt_never_carries_delivery_mode(self):
        """任意交付模式下，派活提示词都不含模式字样。"""
        from shiftboss.taskbook import Taskbook, new
        for index, (kind, mode) in enumerate((('task', None), ('repair', None), ('task', '连做 15'))):
            name = f'probe-{index}'
            new(self.root, name, kind=kind)
            fields = {'你的工作副本': str(self.copy), '状态': '待派', '完成标准': '原型', **({'交付模式': mode} if mode else {})}
            Taskbook.find(self.root, name).set_meta(**fields)
            with patch.object(dispatch, 'REPO', self.root), \
                 patch.object(dispatch, 'read_quota', return_value=5), \
                 patch.object(dispatch, 'load_sessions', return_value={}), \
                 patch.object(dispatch, 'save_sessions'), \
                 patch.object(codex_worker, 'rollout_of', return_value=None), \
                 patch.object(dispatch, 'executor_processes', return_value=[]), \
                 patch.object(dispatch, 'launch_tui') as launch:
                output = io.StringIO()
                with redirect_stdout(output):
                    result = dispatch.main([name, '--dry-run'])
                text = output.getvalue()
                prompt = next((line for line in text.splitlines() if line.startswith('提示词')), '')
                self.assertEqual(result, 0, text)
                self.assertIn('开始新任务：按工作流读', prompt, text)
                for word in ('连做', '自走', '单轮', 'pass'):
                    self.assertNotIn(word, prompt, text)
                launch.assert_not_called()

    def test_rollout_latest_sandbox(self):
        sid = '01a080a6-a16c-77d2-a8f8-de71c03c51cd'
        sample = self.root/'sample.jsonl'
        shutil.copyfile(Path(__file__).parent/'fixtures/foreign-sandbox.jsonl', sample)
        ledger = {'probe': {'session_id':sid, 'copy':str(self.copy)}}
        # The inbox now reads refs directly: give it a real empty origin,
        # preserving the sandbox assertions instead of mocking away the warning.
        origin = self.root/'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
        with patch.object(codex_worker, 'rollout_of', return_value=sample):
            warning = '\n'.join(dispatch.sandbox_warnings(ledger))
            self.assertIn(sid, warning)
            self.assertIn(str(self.copy), warning)
            self.assertIn('workspace-write', warning)
            # Exercise the everyday inbox routes, not only the parser.
            from shiftboss import inbox, verify
            with patch.object(dispatch, 'load_sessions', return_value=ledger), \
                 patch.object(dispatch, 'read_quota', return_value=5), \
                 patch.object(inbox, 'survey', return_value=[]), \
                 patch.object(inbox, 'stalled_lines', return_value=[]), \
                 patch.object(inbox, 'report_merged_idle'), \
                 patch.object(inbox, 'origin_git_dir', return_value=origin), \
                 patch.object(inbox, 'load_acked', return_value={}), \
                 patch.object(inbox, '_git', return_value='probe'), \
                 patch.object(verify, 'show_report', return_value='test report'):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox.main(['list']), 0)
                self.assertIn(sid, output.getvalue())
                self.assertIn(sid, inbox.write_status(self.root, origin))
            rows = sample.read_text(encoding='utf-8').splitlines()
            last = json.loads(rows[-1]);last['payload']['sandbox_policy']={'type':'danger-full-access'}
            rows[-1] = json.dumps(last)
            sample.write_text('\n'.join(rows)+'\n', encoding='utf-8')
            self.assertEqual(dispatch.sandbox_warnings(ledger), [])

if __name__=='__main__': unittest.main()
