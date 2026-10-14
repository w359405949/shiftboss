"""End-to-end acceptance using real local Git, generation, tests and packaging."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from shiftboss.outputs import CATALOG, catalog_document
from shiftboss.state import read_state, write_state
from shiftboss.verify import Verifier, claim, show_report, state_dir
from shiftboss.config import control_dir

ROOT=Path(__file__).resolve().parents[2]

class VerificationAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.repo=base/'source';self.repo.mkdir();self.remote=base/'origin.git';self.workspace=base/'worker'
        shutil.copy2(Path(__file__).resolve().parent/'fixtures'/'shiftboss.toml', self.repo/'shiftboss.toml')
        self.git('init','-q','-b','main');self.git('config','user.name','Acceptance');self.git('config','user.email','acceptance@localhost')
        (self.repo/'.gitignore').write_text('out/\n__pycache__/\n')
        (self.repo/'value.txt').write_text('good')
        (self.repo/'asset.json').write_text(json.dumps({'value':'good'}))
        (self.repo/'producer.py').write_text("from pathlib import Path\nimport json\nfrom shiftboss.outputs import producer_main, declare_outputs\n@producer_main\ndef main():\n p=Path.cwd()/'asset.json'\n declare_outputs([p])\n p.write_text(json.dumps({'value':Path('value.txt').read_text()}))\n return 0\nraise SystemExit(main())\n")
        (self.repo/'checks').mkdir();(self.repo/'checks/__init__.py').touch()
        (self.repo/'checks/test_ok.py').write_text('import unittest\nclass Check(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
        (self.repo/'checks/test_payload.py').write_text("import unittest\nfrom pathlib import Path\nclass Check(unittest.TestCase):\n def test_value(self): self.assertEqual(Path('value.txt').read_text(),'good')\n")
        task=self.repo/'shiftboss/plans/repair.md';task.parent.mkdir(parents=True);task.write_text('# Repair\n\n| 状态 | 在办 |\n')
        steps=[['python','-B','producer.py']]
        plan={'steps':steps,'gates':[['python','-c','print("structural")']],
              'verification':{'test_directories':['checks'],'build':[['python','-c',"from pathlib import Path; Path('out/package.bin').write_bytes(Path('value.txt').read_bytes())"]]}}
        (self.repo/'regen.json').write_text(json.dumps(plan))
        (self.repo/CATALOG).parent.mkdir(parents=True,exist_ok=True)
        (self.repo/CATALOG).write_text(json.dumps(catalog_document([{'command':steps[0],'files':['asset.json']}])))
        self.commit('baseline')
        subprocess.run(['git','init','--bare','-q',str(self.remote)],check=True)
        self.git('remote','add','origin',str(self.remote));self.git('push','-q','origin','main')
    def git(self,*args):
        p=subprocess.run(['git','-C',str(self.repo),*args],capture_output=True,text=True,encoding='utf-8')
        if p.returncode: raise AssertionError(p.stderr)
        return p.stdout.strip()
    def commit(self,message):
        self.git('add','--all');self.git('commit','-qm',message);return self.git('rev-parse','HEAD')
    def remote_head(self):
        return subprocess.check_output(['git',f'--git-dir={self.remote}','rev-parse','main'],text=True).strip()
    def wait_doorbell(self,token,timeout=20):
        """等门铃真的响过这一条。

        **不要读一次就断言**：`verify` 是先把报告写进 `runs/<sha>.json`、再 `ring()`，
        而 `wait_report` 只等报告文件，所以「报告已在、门铃还没响」是正常的一瞬。
        2026-09-09 这条断言就是这么红的（门铃里只有上一轮的绿行），
        改成盯 tip 之后这一瞬变宽了，才暴露出来。**顺序不能反**——
        先响铃后写报告的话，门铃会指向一份还不存在的报告。
        """
        path=control_dir(self.remote)/'bell.log'
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if path.exists() and token in path.read_text(encoding='utf-8'): return
            time.sleep(.05)
        self.fail(f'门铃没有在 {timeout}s 内响过 {token}：'
                  +(path.read_text(encoding='utf-8') if path.exists() else '(门铃文件不存在)'))
    def wait_report(self,source,process,timeout=45):
        """等这一轮的报告落盘且索引已前进。"""
        deadline=time.monotonic()+timeout
        path=state_dir(self.remote)/'runs'/f'{source}.json'
        while time.monotonic()<deadline:
            if path.exists():
                index=read_state(state_dir(self.remote)/'index.json')
                report=read_state(path) if index.get('last') else None
                if report and 'attribution' not in report: return report
            if process.poll() is not None:
                self.fail('watch exited before report:\n'+(Path(self.temp.name)/'watch.log').read_text(encoding='utf-8'))
            time.sleep(.1)
        self.fail('watch did not report within deadline')
    def test_watch_red_attribution_and_claim(self):
        log=Path(self.temp.name)/'watch.log'
        with log.open('w',encoding='utf-8') as output:
            process=subprocess.Popen([sys.executable,'-B','-m','shiftboss.verify','watch','--repo',str(self.repo),'--workspace',str(self.workspace),'--publish','--interval','.1'],cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                baseline=self.git('rev-parse','HEAD');first=self.wait_report(baseline,process)
                self.assertEqual(first['failures'],[]);self.assertEqual(first['tests'],2)
                self.git('fetch','-q','origin','main');self.git('merge','--ff-only','origin/main')
                (self.repo/'value.txt').write_text('bad');bad=self.commit('deliberately break payload');start=time.monotonic();self.git('push','-q','origin','main')
                report=self.wait_report(bad,process);latency=time.monotonic()-start
                self.assertEqual(len(report['failures']),1,report)
                failure=report['failures'][0]
                self.assertEqual(failure['batch_window']['high'],bad)
                self.assertEqual(len(report['test_files']),2)
                self.assertEqual(report['publication'],'已快进补齐 main')
                self.assertEqual(self.remote_head(),report['generated_commit'])
                self.assertEqual((self.workspace/'out/package.bin').read_text(),'bad')
                self.wait_doorbell(bad)
                with self.assertRaisesRegex(ValueError,'派活记录'): claim(self.remote,failure['ticket'],'repair')
                write_state(control_dir(self.remote)/'sessions.json',{'repair':{'session_id':'acceptance-session','closed':False}})
                claim(self.remote,failure['ticket'],'repair')
                inbox=subprocess.run([sys.executable,'-B','-m','shiftboss.inbox','list'],cwd=self.repo,capture_output=True,text=True,encoding='utf-8',timeout=30)
                self.assertEqual(inbox.returncode,0,inbox.stderr)
                shown=inbox.stdout;self.assertIn('repair / 会话 acceptance-session',shown)
                print(f'REAL WATCH: bad={bad} detected_in={latency:.3f}s\n{shown}')
                # A generated commit is already tested, so it must not generate an endless loop.
                count=len(list((state_dir(self.remote)/'runs').glob('*.json')))
                time.sleep(.4)
                self.assertEqual(len(list((state_dir(self.remote)/'runs').glob('*.json'))),count)
                self.git('fetch','-q','origin','main');self.git('merge','--ff-only','origin/main')
                (self.repo/'value.txt').write_text('good');fixed=self.commit('fix payload')
                (self.repo/'value.txt').write_text('bad');regression=self.commit('intermediate regression')
                (self.repo/'value.txt').write_text('good');final=self.commit('fix regression')
                self.git('push','-q','origin','main')
                final_report=self.wait_report(final,process)
                self.assertFalse((state_dir(self.remote)/'runs'/f'{regression}.json').exists())
                self.assertEqual(final_report['covered_count'],3)
                self.assertEqual(final_report['failures'],[])
                self.assertIn('已恢复',(state_dir(self.remote)/'findings'/f"{failure['ticket']}.md").read_text(encoding='utf-8'))
                print(f'BATCH: {fixed[:12]} -> {regression[:12]} -> {final[:12]} (red 0); tip covers 3 commits')
            finally:
                (state_dir(self.remote)/'stop').touch()
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired: process.terminate();process.wait();raise
        self.assertEqual(process.returncode,0,log.read_text(encoding='utf-8'))
    def test_claim_before_finding_is_written(self):
        """报告已发布、筛查未写：认领照样成立，筛查写出时带上认领。"""
        (self.repo/'value.txt').write_text('bad');bad=self.commit('deliberately break payload');self.git('push','-q','origin','main')
        worker=Verifier(self.repo,self.workspace,publish=False)
        failure=worker.run(bad,False)['failures'][0]
        finding=state_dir(self.remote)/'findings'/f"{failure['ticket']}.md"
        finding.unlink()
        write_state(control_dir(self.remote)/'sessions.json',{'repair':{'session_id':'early-session','closed':False}})
        claim(self.remote,failure['ticket'],'repair')
        text=worker.finding(failure,bad)
        self.assertIn('| 状态 | 已认领 |',text)
        self.assertIn('| 负责 | repair / early-session |',text)
    def test_stale_generation_merges_onto_new_main(self):
        """main 前进时生成提交三方合并后发布：新源保留，派生物不再饿死。"""
        (self.repo/'asset.json').write_text('{}');old=self.commit('stale published artifact');self.git('push','-q','origin','main')
        worker=Verifier(self.repo,self.workspace,publish=True)
        (self.repo/'value.txt').write_text('newer');new=self.commit('newer source');self.git('push','-q','origin','main')
        report=worker.run(old,False)
        head=self.remote_head()
        self.assertNotEqual(head,new)
        self.assertIn('三方合并',report['publication'])
        self.git('fetch','-q','origin','main')
        self.assertIn(new,self.git('rev-list',head))
        self.assertEqual(self.git('show',f'{head}:value.txt'),'newer')
        self.assertEqual(report['failures'],[])
    def test_new_file_and_import_failure_are_not_omitted(self):
        (self.repo/'checks/test_broken.py').write_text('raise RuntimeError("deliberate import failure")\n')
        report=self.repo/'out/tests.json'
        result=subprocess.run([sys.executable,'-B','-m','shiftboss.verify_tests','--report',str(report),'checks'],cwd=self.repo,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(report.is_file(),result.stderr)
        data=read_state(report)
        self.assertEqual(len(data['files']),3)
        self.assertEqual(data['tests'],3)
        self.assertEqual(len(data['failures']),1)
        self.assertIn('deliberate import failure',data['failures'][0]['detail'])

    def batch(self, count, break_at=None):
        commits=[]
        for number in range(1,count+1):
            (self.repo/'sequence.txt').write_text(str(number))
            if number == break_at: (self.repo/'value.txt').write_text('bad')
            commits.append(self.commit(f'batch {number}'))
        self.git('push','-q','origin','main')
        return commits

    def test_green_tip_covers_backlog(self):
        worker=Verifier(self.repo,self.workspace)
        worker.watch(once=True)
        commits=self.batch(8)
        self.assertIn('还差 8 个第一父链提交',show_report(self.remote))
        before=len(list((worker.directory/'logs').glob('*-tests.log')))
        worker.watch(once=True)
        after=len(list((worker.directory/'logs').glob('*-tests.log')))
        self.assertEqual(after-before,1)
        self.assertEqual(worker.index['last'],commits[-1])
        self.assertEqual(worker.index['last_green'],commits[-1])
        report=read_state(worker.directory/'runs'/f'{commits[-1]}.json')
        self.assertEqual(report['covered_count'],8)
        worker.watch(once=True)
        self.assertEqual(len(list((worker.directory/'logs').glob('*-tests.log'))),after)
        self.assertIn('还差 0 个第一父链提交',show_report(self.remote))
        print('TIP BACKLOG: N=8, suite executions=1, remaining=0')

    def test_existing_baseline_red_stays_unknown(self):
        (self.repo/'value.txt').write_text('bad');self.commit('baseline red')
        self.git('push','-q','origin','main')
        worker=Verifier(self.repo,self.workspace)
        worker.watch(once=True)
        first=read_state(worker.directory/'runs'/f"{worker.index['last']}.json")
        self.batch(5)
        worker.watch(once=True)
        last=read_state(worker.directory/'runs'/f"{worker.index['last']}.json")
        self.assertIsNone(last['failures'][0]['introduced_by'])
        self.assertEqual(first['failures'][0]['ticket'],last['failures'][0]['ticket'])
        self.assertEqual(len(list((worker.directory/'logs').glob('*-tests.log'))),2)

    def test_generation_skips_without_generation_input_changes(self):
        """生成输入没动就不重算派生物；动了照常生成。改回无条件生成时 `skipped` 断言失败。"""
        plan=json.loads((self.repo/'regen.json').read_text())
        plan['gates'].append(['python','-B','-m','unittest','checks.test_ok','checks.test_payload'])
        plan.update(gate_layers={'publish':{'gates':[0,1],'generate':True}},always_layers=['publish'],test_root='checks',
                    path_layers={'producer.py':['publish'],'value.txt':['publish'],'docs/':[]})
        (self.repo/'regen.json').write_text(json.dumps(plan));self.commit('declare generation inputs')
        self.git('push','-q','origin','main')
        worker=Verifier(self.repo,self.workspace,publish=True)
        worker.watch(once=True)
        self.assertEqual(worker.index['last_consistent'],worker.index['last'])
        self.git('fetch','-q','origin','main');self.git('merge','--ff-only','origin/main')
        (self.repo/'docs').mkdir(exist_ok=True);(self.repo/'docs/note.md').write_text('docs only')
        docs=self.commit('docs only');self.git('push','-q','origin','main')
        worker.watch(once=True)
        self.assertIn('skipped',read_state(worker.directory/'runs'/f'{docs}.json')['stages']['generate'])
        self.git('fetch','-q','origin','main');self.git('merge','--ff-only','origin/main')
        (self.repo/'value.txt').write_text('changed');changed=self.commit('generation input');self.git('push','-q','origin','main')
        worker.watch(once=True)
        self.assertNotIn('skipped',read_state(worker.directory/'runs'/f'{changed}.json')['stages']['generate'])

if __name__=='__main__': unittest.main()
