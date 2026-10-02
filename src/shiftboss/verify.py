"""常驻验证：在一个自有 checkout 里验证 main tip。

    shiftboss verify watch --workspace <新目录> --publish
    shiftboss verify status
    shiftboss verify claim <票据ID> <已派出的修复任务书名>
    shiftboss verify stop

**这是基础设施，和门铃并列**：合并不再跑生成与测试，全都挪到这里。
报告与修复票据放在控制目录的 `verification/`，`inbox` 能看见。

**`--workspace` 必须是新目录或本验证器创建并标记过的独立 clone**，
不能指向执行副本——执行方随时在自己的副本里写字。
按 origin 加独占锁；确认旧进程已结束才能清理遗留锁。

**先验 tip，新红再沿第一父链二分归因。** 绿 tip 覆盖整段积压，不逐提交跑；
新红以最近一次没有该失败的可比报告为下界；已有红沿用票据与未知基线。
二分针对失败持续存在的区间，不证明跳过的历史从未短暂变红；非单调或不稳定
测试的全历史首次失败不能靠 O(log N) 次运行保证。
归因探针只跑红测试所在的测试文件，不跑闸与打包；定位不到文件的红照跑全量。
历史被改写时重新建立基线，并把归因与趋势标为未知。
`stop` 等当前提交验完再停，再次启动从最后报告继续。
`--once` 验一次当前 tip（含新红归因）就退出；不带 `--publish` 只存报告与本地生成提交，不补 main。

**跑什么**：`regen.json` 的 `verification.test_directories` 声明全量测试目录，
文件由每次运行现发现；每个 `test_*.py` 都载入，**导入异常也变成失败记录**，
不中断后续文件；跳过的测试单列，不冒充已执行。
先生成，再跑全部测试、未被全量套件覆盖的原闸、以及 `verification.build` 打包命令；
**普通测试红不阻止其余阶段**。生成失败同样报告，已有结果不冒充新生成结果。
生成输入自上次派生物一致的提交以来没有改动时跳过生成（判法见 `generation_needed`）。
验证用固定源提交；测试驱动按内容哈希快照，避免控制副本更新时中途换驱动。

**补派生物**：生成只准改生产端报告拥有的路径，生成提交只有源 SHA 一个父节点；
测试后工作树有变化则不发布。main 未前进时 `--publish` 做普通快进 push。
**main 前进了就把生成提交三方合并到最新 main 再推**：只等 main 不动会饿死——
一次生成三四十分钟，而主线要求最快合并，空窗极少；派生物因此长期停在旧版本，
而验证报告全绿，没有任何症状。合并只在对象库里做，不动工作树；
两侧都改过的路径算冲突，本轮不发、由下一轮以更新的源重算，**绝不覆盖更新的源**。
派生物因此可能比源码略旧一轮，**这是这套设计接受的代价**。

**报红要有人接**：每条新红进派活方待派队列并生成修复任务书草稿，不是无人负责的日志。
派活方在 `shiftboss/plans` 开任务书、正常派出，再用 `claim` 关联；
没有 main 上的任务书、没有有效会话号、或派活记录已关闭的，认领会拒绝。
**红不自动阻断合并**，未认领的红持续显示负责方与待派状态。

可复现判据：`python -B -m unittest test_verify`。
"""
from __future__ import annotations
from shiftboss.paths import PLANS_DIR, PROJECT_DIR, project_root
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from shiftboss import config
from shiftboss.outputs import CATALOG, catalog_files
from shiftboss.state import read_state, write_state

ROOT = project_root()
NAME = 'verification'
SCHEMA = 'workflow.verification.v1'

# 一轮里准许花在归因上的时间下限。**tip 验证优先于归因，这条不能反过来。**
#
# 每个探针是一次 generate ＋ 红测试所在文件（`run(..., probe=True, only=...)`；
# 有定位不到文件的红时退回全量测试 ＋ 闸 ＋ 打包）。全量探针几小时一个；而 `bisect_failures` 原来对**每一条红**都做一次二分，没有上限。
# 红有几十条、范围有上百个提交时它跑不完，于是 **tip 那份报告永远写不出去、
# `index.last` 永远不前进**——外面看到的是「已验到 <两天前那个提交>，离 tip 还差 N 个」，
# 而「红不阻断合并、验证线兜底」这张网其实一直没盖到当前 main。
# 2026-09-12 实测：最后一次真 tip 验证停在两天前，之后全是探针。
#
# **归因可以慢，当前信号不能停。** 预算用尽就把这一条收窄到的区间记在票上、
# 让这一轮的 tip 报告落盘，下一轮从那个区间接着二分。
#
# 上限按时间取，取这一轮 tip 自己花掉的秒数（不低于本值）：探针跑的和 tip 是同一套
# 东西，一个探针≈一次 tip，**于是归因最多把一轮拖成两轮**。跑得快的仓库撞不到这条线，
# 慢的仓库自动降到一轮一个探针——同一条规矩不用按仓库大小重调。
PROBE_BUDGET_FLOOR = 60


def generation_needed(plan: dict, paths: list[str]) -> bool:
    """改动里有生成输入才重新生成；没有路径规则时一律生成。

    生成输入是 `path_layers` 中归到带 `generate` 层的路径；测试目录只是消费端，不算输入。
    """
    rules = plan.get('path_layers')
    if not rules:
        return True
    generating = {name for name, layer in (plan.get('gate_layers') or {}).items() if layer.get('generate')}
    test_root = (plan.get('test_root') or '').rstrip('/')
    for path in paths:
        if test_root and path.startswith(test_root + '/'):
            continue
        matches = [prefix for prefix in rules
                   if path == prefix or (prefix.endswith('/') and path.startswith(prefix))]
        if not matches or set(rules[max(matches, key=len)]) & generating:
            return True
    return False


def failure_module(failure_id: str):
    """红所在的测试模块；阶段失败或加载器合成的失败定位不到文件，返回 None。"""
    if failure_id.startswith(('stage:', 'unittest.')):
        return None
    parts = failure_id.split(' ', 1)[0].split('.')
    for index, part in enumerate(parts[:-1]):
        if part.startswith('test_'):
            return '.'.join(parts[:index + 1])
    return None


def git(repo: Path, *args: str, check=True):
    p = subprocess.run(['git','-C',str(repo),*args], capture_output=True, text=True, encoding='utf-8')
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p


def origin(repo: Path) -> Path:
    url = git(repo,'remote','get-url','origin').stdout.strip()
    path = Path(url)
    if not path.is_absolute(): path = repo/path
    path = path.resolve()
    if not path.is_dir() or git(path,'rev-parse','--is-bare-repository').stdout.strip() != 'true':
        raise ValueError('验证进程需要本机 bare origin')
    return path


def state_dir(remote: Path) -> Path:
    return config.control_dir(remote)/NAME


def index_of(remote: Path) -> dict:
    data = read_state(state_dir(remote)/'index.json')
    if data and data.get('schema') != SCHEMA:
        raise ValueError('无法读取验证报告版本；不能当成未运行')
    return data


from shiftboss.doorbell import ring  # 门铃（文件名、hook、写入）只在 shiftboss.doorbell 一处


def first_parent_range(repo: Path, last: str | None, tip: str):
    if not last: return None
    if last == tip: return []
    if git(repo,'merge-base','--is-ancestor',last,tip,check=False).returncode: return None
    commits=git(repo,'rev-list','--first-parent','--reverse',f'{last}..{tip}').stdout.split()
    parents=git(repo,'show','-s','--format=%P',commits[0]).stdout.split() if commits else []
    return commits if parents and parents[0] == last else None


def stale_worker_code(remote: Path, index: dict) -> str:
    """常驻进程比它自己的源码还老时喊一声。

    **改了 `verify.py` 不会让正在跑的那个进程变聪明**——它加载的是启动那一刻
    的代码，之后一直用那一份。这件事没有任何症状：报告照出、门铃照响、日志照写，
    只是里面全是旧行为，而人看着「它在跑」就以为修好了。
    **一次这样的误判花掉四个半小时**：三处改动都没生效，进程还在按旧预算做二分。

    判据只要一条：进程起得比源码的最后一次改动早，就是旧代码。
    """
    pid=index.get('pid')
    if not pid: return ''
    try:
        import psutil
        started=psutil.Process(pid).create_time()
    except Exception:
        return ''
    from shiftboss.paths import package_dir_in
    package=package_dir_in(ROOT)
    if not package: return ''
    source=f'{package}/src/shiftboss/verify.py'
    changed=git(remote,'log','-1','--format=%ct','refs/heads/main','--',source,check=False).stdout.strip()
    if not changed.isdigit() or int(changed) <= started: return ''
    return ('  **常驻验证在跑旧代码**：PID {pid} 起于 {source} 最后一次改动之前，'
            '那次改动对它无效。**这一句只报事实，不是要你立刻重起**——'
            '改的是显示措辞就不必打断一轮，改的是判据或预算就得重起'
            '（`shiftboss verify stop --now` 再 `start`）。').format(pid=pid,source=source)


def show_report(remote: Path) -> str:
    index = index_of(remote)
    current = git(remote,'rev-parse','refs/heads/main').stdout.strip()
    if not index:
        return f'后台验证：main@{current[:12]} 尚无验证报告；已验到 未知；离 tip 还差 未知 个第一父链提交；派活方需启动 shiftboss.verify watch。'
    latest = index.get('last')
    pending = first_parent_range(remote,latest,current)
    lines = [f'后台验证：main@{current[:12]} ' + ('已验证' if current == latest else '待验证（不能以旧报告报绿）')]
    # **这一行只在「上次验的那个提交确实在 main 的第一父链上」时才有意义。**
    # 验证器发布补齐时会造自己的生成提交，那种提交不在 main 上，
    # 拿它当参照算出来的「还差 N 个」是个大得吓人又没意义的数——
    # 2026-09-09 我据此以为验证器在逐提交爬，其实它每轮都取当前 tip。
    if pending is not None:
        gap = f"离 tip 还差 {len(pending)} 个第一父链提交"
    elif latest:
        gap = "上次验的提交不在 main 的第一父链上（多半是补齐用的生成提交），差距算不出"
    else:
        gap = "还没有基线，差距算不出"
    active = index.get('active') or {}
    if active.get('commit') and active['commit'] != latest:
        gap += f"；正在验 {active['commit'][:12]}"
    lines.append(f"  已验到 {(latest or '未知')[:12]}；{gap}。")
    lock=state_dir(remote)/'worker.lock'
    if not lock.exists(): lines.append('  常驻验证当前未持锁；派活方需确认进程已启动。')
    else:
        # 重启或崩溃后锁文件留着、进程没了：「正在验」是旧状态，不能当作在跑。
        import psutil
        holder=lock.read_text(encoding='utf-8').strip()
        if not (holder.isdigit() and psutil.pid_exists(int(holder))):
            lines.append(f'  常驻验证进程 PID {holder or "无"} 已不在，以下「正在验」为旧状态；重起：shiftboss verify start --publish')
    # **门铃停了没有任何东西会说。** 它每响一次就退出，要派活方重挂；
    # 忙别的事忘了重挂时，推送照常进来而这边一无所知——2026-09-21 就这么漏掉了
    # 四条线共六段。常驻验证有「未持锁」这条提醒，门铃没有，这里补齐。
    try:
        from shiftboss.doorbell import listening
        if not listening():
            from shiftboss.doorbell import REARM_COMMAND
            lines.append(f'  门铃当前没人听；重挂：{REARM_COMMAND}')
    except Exception:
        pass
    stale=stale_worker_code(remote,index)
    if stale: lines.append(stale)
    if index.get('active'):
        active=index['active']; lines.append(f"  执行记录：{active['commit'][:12]} {active['stage']}，PID {index.get('pid')}，始于 {active['started']}")
    if index.get('error'): lines.append('  验证进程异常，责任：派活方：'+index['error'])
    if not latest: return '\n'.join(lines)
    report=read_state(state_dir(remote)/'runs'/f'{latest}.json')
    failures=report['failures']; claims=read_state(state_dir(remote)/'claims.json')
    trend = f"较上次验证 {report['delta']:+d}" if report['delta'] is not None else '起始基线，无可比前序'
    lines.append(f"  最近 {latest[:12]}：红 {len(failures)} 条，{trend}；测试文件 {len(report.get('test_files', []))}，测试 {report.get('tests', 0)}，跳过 {len(report.get('skipped', []))}。")
    lines.append(f"  生成提交：{report.get('generated_commit') or '未完成'}；补齐：{report.get('publication','未补齐')}")
    for item in failures:
        claim=claims.get(item['ticket'])
        owner=(f"{claim['taskbook']} / 会话 {claim['session_id']}" if claim else '派活方（待开修复任务书并派出）')
        onset=item.get('introduced_by') or item.get('attribution_error') or '基线已红，起点未知'
        age=max(0,int(time.time()-item['first_seen']))
        lines.append(f"  [{item['ticket']}] {item['id']}；从 {onset}；已观察红 {age}s；负责：{owner}")
        if item.get('incoming'): lines.append('    合入父提交：'+', '.join(item['incoming']))
        lines.append(f"    筛查：{state_dir(remote)/'findings'/(item['ticket']+'.md')}")
    if not failures and report.get('skipped'):
        lines.append('  有跳过项，不声称所有测试都已执行；明细见运行报告。')
    return '\n'.join(lines)


def claim(remote: Path, ticket: str, taskbook: str):
    if Path(taskbook).name != taskbook:
        raise ValueError('任务书给文件名，不带目录或扩展名')
    index=index_of(remote)
    if not index.get('last'): raise ValueError('没有可认领的验证报告')
    report=read_state(state_dir(remote)/'runs'/f"{index['last']}.json")
    if ticket not in {x['ticket'] for x in report['failures']}:
        raise ValueError('这条红已不在最近报告中')
    # 两种布局都要认；布局的事全归 shiftboss.taskbook。
    from shiftboss.taskbook import from_ref
    def _read(rel):
        out=git(remote,'show',f'refs/heads/main:{rel}',check=False)
        return '' if out.returncode else out.stdout
    meta,_body=from_ref(_read,taskbook)
    ledger=read_state(config.control_dir(remote)/'sessions.json')
    entry=ledger.get(taskbook,{})
    if meta is None or not entry.get('session_id') or entry.get('closed'):
        raise ValueError('认领拒绝：需要 main 上的修复任务书，以及未关闭、有会话号的派活记录')
    claims=read_state(state_dir(remote)/'claims.json')
    claims[ticket]={'taskbook':taskbook,'session_id':entry['session_id'],'claimed':time.time()}
    write_state(state_dir(remote)/'claims.json',claims)
    # 二分归因期间报告已发布、筛查还没写；那时认领只记 claims.json，筛查写出时照它填。
    path=state_dir(remote)/'findings'/f'{ticket}.md'
    if path.is_file():
        text=path.read_text(encoding='utf-8').replace('| 状态 | 待派 |','| 状态 | 已认领 |').replace('| 负责 | 派活方 |',f"| 负责 | {taskbook} / {entry['session_id']} |")
        path.write_text(text,encoding='utf-8')
    ring(remote,f'claimed {ticket} {taskbook}')
    print(f"已认领 {ticket} → {taskbook} / 会话 {entry['session_id']}")


def taskbooks_touching(remote: Path, paths: list[str]) -> list[str]:
    """`main` 上哪些在办任务书的正文提到了这些路径。**候选，不是结论。**

    派活方最贵的判断是「这条红归谁、是不是已经有线在动那片代码」。
    验证器算得出前半截：引入提交改了哪些路径、哪些任务书写着这些路径。
    **它给候选，派活方定夺**——任务书里那几行范围是自由文本，匹配到不等于归属。
    """
    # -r 才能看进目录布局的 <name>/contract.md；单文件布局照旧命中 <name>.md。
    listing=git(remote,'ls-tree','-r','--name-only','refs/heads/main',PLANS_DIR+'/',check=False)
    hits=[]
    for name in listing.stdout.split():
        if not name.endswith('.md'): continue
        # 名为「在办」：归档的线已收摊，列进来只会淹没真正该看的那几条。
        if f'{PLANS_DIR}/archive/' in name: continue
        text=git(remote,'show',f'refs/heads/main:{name}',check=False)
        if text.returncode: continue
        if not any(path in text.stdout for path in paths): continue
        rel=Path(name)
        # 目录布局：shiftboss/plans/<name>/contract.md → 线名是那个目录名。
        stem=rel.parent.name if rel.parent.name not in ('plans','archive') else rel.stem
        if stem not in hits: hits.append(stem)
    return hits


def flaky_history(directory: Path, failure_id: str, limit=8) -> str:
    """这条红在最近几份报告里是一直红，还是红一阵好一阵。

    **间歇红和稳定回归要分开报**：按「稳定回归」去开一条修复线，
    而实际是竞态或环境抖动，那条线只会带回来一句「本地跑是绿的」。
    2026-09-09 `test_watch_red_attribution_and_claim` 就是这样——
    Windows 原子改名撞并发读者，间歇的，却挂了 53 分钟才被看出来。
    """
    reports=sorted((directory/'runs').glob('*.json'), key=lambda q: q.stat().st_mtime)[-limit:]
    seen=[]
    for path in reports:
        try: data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError): continue
        if 'failures' not in data: continue
        seen.append(any(f.get('id')==failure_id for f in data['failures']))
    if len(seen)<3: return f'样本不足（最近 {len(seen)} 份报告），先当未知'
    flips=sum(1 for a,b in zip(seen,seen[1:]) if a!=b)
    shape=''.join('红' if x else '绿' for x in seen)
    return (f'**间歇**（最近 {len(seen)} 份：{shape}，翻转 {flips} 次）——按稳定回归开线多半白开'
            if flips>1 else f'稳定（最近 {len(seen)} 份：{shape}）')


CONFLICT_TOKENS = ('CONFLICT-KEPT', '<<<<<<< ', '>>>>>>> ')
"""合并时留下的东西，**不该活到常驻验证这一步**。

`CONFLICT-KEPT` 是合并方拿不准时留下的落选分支（规矩见 `shiftboss/README.md` 第 2 节）：
留着是对的——比记在脑子里强——但它的归宿是被那份核验任务清掉。
`<<<<<<<` / `>>>>>>>` 更简单：真冲突标记进了提交，说明有人把半解的合并交了出去。

**这一层是兜底，不是主路**：主路是合并方当场决断、核验任务交付时清干净。
兜底只负责让它无处可藏——报红、点名文件，由派活方处理。
"""


def conflict_leftovers(workspace: Path) -> list[str]:
    """工作副本里还留着合并残留的文件。**只看 git 跟踪的文本文件。**"""
    listing = git(workspace, 'ls-files', '-z', check=False)
    from shiftboss.paths import package_dir_in
    package = package_dir_in(ROOT)
    skip = (PROJECT_DIR + '/',) + ((package + '/',) if package else ())
    hits = []
    for name in listing.stdout.split('\0'):
        if not name or name.startswith(skip):
            # 任务书、工作流文档与 shiftboss 包本身会引用这些词（规矩就写在那儿），不是残留。
            continue
        path = workspace / name
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for token in CONFLICT_TOKENS:
            if token in text:
                hits.append(f'{name}：{token}')
                break
    return sorted(hits)


class Verifier:
    def __init__(self, repo: Path, workspace: Path, publish=False):
        self.repo=repo.resolve(); self.remote=origin(self.repo)
        self.workspace=workspace.resolve(); self.publish=publish
        self.probe_budget=None     # None＝按 tip 耗时自动定；测试用它把预算摁死
        if self.workspace == self.repo or self.workspace == self.remote:
            raise ValueError('验证副本必须独立')
        self.directory=state_dir(self.remote)
        for name in ('runs','findings','logs'): (self.directory/name).mkdir(parents=True,exist_ok=True)
        legacy=self.directory/'repairs'
        if legacy.is_dir():
            # 旧名字叫 repairs，内容却不是任务书。改名的同时把旧票据搬过来，别丢历史。
            for old_ticket in legacy.glob('*.md'):
                target=self.directory/'findings'/old_ticket.name
                if not target.exists(): old_ticket.replace(target)
            if not any(legacy.iterdir()): legacy.rmdir()
        self.index=index_of(self.remote) or {'schema':SCHEMA}
        self.index['pid']=os.getpid()
        self.policy_commit=git(self.repo,'rev-parse','HEAD').stdout.strip()
        driver=(Path(__file__).resolve().with_name('verify_tests.py')).read_bytes()
        self.driver_hash=hashlib.sha256(driver).hexdigest()
        controllers=self.directory/'controllers';controllers.mkdir(exist_ok=True)
        self.driver=controllers/(self.driver_hash+'.py')
        if self.driver.exists():
            if self.driver.read_bytes()!=driver: raise ValueError('验证驱动快照损坏')
        else:
            temporary=controllers/(self.driver_hash+f'.{os.getpid()}.tmp')
            temporary.write_bytes(driver);os.replace(temporary,self.driver)
        self.policy=json.loads((self.repo/config.plan_file(self.repo)).read_text(encoding='utf-8')).get('verification')
        if not self.policy: raise ValueError(f'{config.plan_file(self.repo)} 缺少 verification 后台策略')
        self.prepared=False

    def prepare(self):
        if self.prepared: return
        marker=self.workspace/'.git/workflow-verifier-owned'
        if self.workspace.exists():
            if not marker.is_file() or marker.read_text(encoding='utf-8') != str(self.remote):
                raise ValueError('拒绝接管不是本验证器创建的工作副本')
        else:
            git(self.repo,'clone','--no-checkout',str(self.remote),str(self.workspace))
            marker.write_text(str(self.remote),encoding='utf-8')
        git(self.workspace,'config','user.name','Workflow verifier')
        git(self.workspace,'config','user.email','workflow-verifier@localhost')
        name=config.local_config_file(self.repo)
        local=self.repo/name if name else None
        if local and local.is_file(): shutil.copyfile(local,self.workspace/local.name)
        self.prepared=True

    def save(self):
        write_state(self.directory/'index.json',self.index)

    def current(self):
        git(self.workspace,'fetch','--quiet','origin','main')
        return git(self.workspace,'rev-parse','FETCH_HEAD').stdout.strip()

    def publish_onto(self, tip: str, generated: str) -> str:
        """main 在生成期间前进时，把生成提交三方合并到最新 main 再推。

        只等「main 不动」会饿死：一次生成几十分钟，而合并几分钟一次，派生物就永远发不出去。
        两边都改过的路径算冲突，本轮不发，由下一轮以更新的源重新生成；合并只在对象库里做，不动工作树。
        """
        merged=git(self.workspace,'merge-tree','--write-tree',tip,generated,check=False)
        if merged.returncode:
            return 'main 已前进且与生成提交冲突，本轮不推送；等待最新源重新生成'
        tree=merged.stdout.split()[0]
        message=f'派生物：合入 {generated[:12]} 的生成结果'
        commit=git(self.workspace,'commit-tree',tree,'-p',tip,'-p',generated,'-m',message).stdout.strip()
        pushed=git(self.workspace,'push','origin',f'{commit}:refs/heads/main',check=False)
        if pushed.returncode:
            return 'main 在合并期间再次前进，推送被拒绝；下一轮重试'
        return f'main 已前进，生成提交已三方合并后推送（{commit[:12]}）'

    def stage(self, sha, stage):
        self.index['active']={'commit':sha,'stage':stage,'started':time.time()}
        self.save()

    def command(self, sha: str, label: str, command: list[str]):
        if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command):
            raise ValueError(f'无效命令：{label}')
        self.stage(sha,label)
        cmd=[sys.executable,*command[1:]] if command[0]=='python' else command
        log=self.directory/'logs'/f'{sha}-{label}.log'
        start=time.monotonic()
        with log.open('w',encoding='utf-8') as output:
            env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
            try:
                code=subprocess.run(cmd,cwd=self.workspace,stdout=output,stderr=subprocess.STDOUT,env=env).returncode
            except OSError as exc:
                output.write(str(exc)+'\n');code=127
        return {'command':command,'returncode':code,'seconds':time.monotonic()-start,'log':str(log)}

    def attribution_work(self, baseline, sha, unique, previous):
        """每条红这一轮在哪一段里二分：新红从上一轮验过的那一点到 tip，
        上一轮没归完的接着它已经收窄到的那一段。

        **少了「接着上次」这一半，预算就不是延后而是丢弃。**
        一条红只在它头一次出现的那一轮属于「新红」；那一轮没归完，之后每一轮它都从
        上一轮抄一个空的 `introduced_by`，再没有人碰它——票上会一直挂着
        「下一轮接着归因」，而那个下一轮永远不来。
        """
        work=[]
        for item in unique.values():
            window=item.get('attribution_window')
            if window: work.append((item,window['low'],window['high']))
            elif item['id'] not in previous: work.append((item,baseline,sha))
        return work

    def budget(self, report):
        if self.probe_budget is not None: return self.probe_budget
        return max(report.get('seconds') or 0, PROBE_BUDGET_FLOOR)

    def bisect_failures(self, work, budget):
        # 探针按「提交 + 模块」缓存：只跑了某个文件的探针不能回答别的文件里的红。
        # 定位得到文件的红先归因，少数定位不到的才用全量探针，不让它拖着全体跑全量。
        work=sorted(work,key=lambda entry: failure_module(entry[0]['id']) is None)
        cache={}
        blocked={}
        result={}
        deadline=time.monotonic()+budget
        for item, low_commit, high_commit in work:
            commits=first_parent_range(self.workspace,low_commit,high_commit)
            if commits is None: continue
            module=failure_module(item['id'])
            low=-1; high=len(commits)-1
            while high-low > 1:
                mid=(low+high)//2; candidate=commits[mid]
                key=(candidate,None) if (candidate,None) in cache else (candidate,module)
                if key not in cache:
                    # **预算用尽就收手**，把收窄到的区间记下来，让 tip 报告落盘。
                    # 理由写在 PROBE_BUDGET_FLOOR 上方：归因可以慢，当前信号不能停。
                    if time.monotonic() >= deadline:
                        pending={'low':low_commit if low<0 else commits[low],'high':commits[high]}
                        item['attribution_window']=pending
                        item['attribution_error']=(
                            f'二分未完成：本轮探针预算已用尽，已收窄到 '
                            f'{pending["low"][:12]}..{pending["high"][:12]}，下一轮接着归因')
                        print(item['attribution_error'],flush=True)
                        break
                    probe=self.run(candidate,False,probe=True,only=None if module is None else [module])
                    cache[key]={f['id'] for f in probe['failures']}
                    blocked[key]=cache[key] & {'stage:infrastructure','stage:generate','stage:tests'}
                red=item['id'] in cache[key]
                if not red and blocked.get(key):
                    # 探针本身没跑起来，不是「这一段里没有这条红」。**这一条要人看**，
                    # 不留区间——留了就是每一轮都去撞同一个跑不起来的提交。
                    item.pop('attribution_window',None)
                    item['attribution_error']=f"二分待议：{candidate} 未能完整验证：{sorted(blocked[key])}"
                    print(item['attribution_error'],flush=True)
                    break
                print(f"二分 {item['id']}：{candidate} {'红' if red else '无此失败'}",flush=True)
                if red: high=mid
                else: low=mid
            else:
                item.pop('attribution_window',None)
                item.pop('attribution_error',None)
                result[item['id']]=commits[high]
                print(f"归因 {item['id']}：{commits[high]}",flush=True)
        return result

    def run(self, sha: str, contiguous: bool, probe=False, only=None):
        self.prepare()
        started=time.time(); previous={}
        if self.index.get('last'):
            old=read_state(self.directory/'runs'/f"{self.index['last']}.json")
            if contiguous: previous={x['id']:x for x in old['failures']}
        report={'schema':SCHEMA,'commit':sha,'source_commit':sha,'started':started,
                'policy_commit':self.policy_commit,'test_runner_sha256':self.driver_hash,'stages':{},'failures':[],
                'publication':'未补齐','generated_commit':None}
        failures=[]; generation_ok=False
        try:
            self.stage(sha,'checkout')
            # This is an exclusively owned disposable clone, never an execution checkout.
            git(self.workspace,'checkout','--detach','--force',sha)
            plan=json.loads((self.workspace/config.plan_file(self.workspace)).read_text(encoding='utf-8'))
            policy=plan.get('verification') or self.policy
            report['policy_commit']=sha if plan.get('verification') else self.policy_commit
            directories=policy['test_directories']
            if not isinstance(directories,list): raise ValueError('测试目录清单必须是列表')
            build=policy['build']
            if not isinstance(build,list): raise ValueError('打包命令清单必须是列表')
            leftovers=conflict_leftovers(self.workspace)
            if leftovers:
                failures.append({'id':'stage:conflict-leftovers',
                                 'detail':'合并残留没清干净（规矩见 shiftboss/README.md 第 2 节）：\n'
                                          +'\n'.join(f'  {x}' for x in leftovers)})
            old_catalog=json.loads((self.workspace/CATALOG).read_text(encoding='utf-8'))
            # 旧清单只用来认定可改写的旧产物，按它自己记录的步骤读；本次改了生成步骤时也成立。
            old_files=set(catalog_files(old_catalog,[p['command'] for p in old_catalog.get('producers') or []]))
            base=self.index.get('last_consistent')
            inputs=git(self.workspace,'diff','--name-only','-z',base,sha,check=False) if base else None
            skip=(inputs is not None and inputs.returncode==0
                  and not generation_needed(plan,[p for p in inputs.stdout.split('\0') if p]))
            if skip:
                # 生成输入自派生物一致的那个提交以来没动：派生物现成，不重算。
                report['stages']['generate']={'skipped':f'自 {base[:12]} 起无生成输入改动','returncode':0,'seconds':0,'log':''}
                generation_ok=True
                report['generated_commit']=sha
            else:
                result=self.command(sha,'generate',['python','-B','-m','shiftboss.regen','--steps'])
                report['stages']['generate']=result
                generation_ok=result['returncode']==0
                if not generation_ok: failures.append({'id':'stage:generate','detail':result['log']})
            if generation_ok and not skip:
                catalog=json.loads((self.workspace/CATALOG).read_text(encoding='utf-8'))
                allowed=old_files | set(catalog_files(catalog,plan['steps']))
                changed=set(git(self.workspace,'diff','--name-only','-z',sha).stdout.split('\0'))-{''}
                changed.update(git(self.workspace,'ls-files','--others','--exclude-standard','-z').stdout.split('\0'))
                changed.discard('')
                if changed-allowed: raise ValueError('生成改了非产物文件：'+', '.join(sorted(changed-allowed)))
                if changed:
                    git(self.workspace,'add','--all')
                    git(self.workspace,'commit','-qm',f'Generate artifacts for {sha}\n\nWorkflow-Generated-From: {sha}')
                report['generated_commit']=git(self.workspace,'rev-parse','HEAD').stdout.strip()
            if directories:
                # Every selected file is loaded, including import failures.
                test_report=self.workspace/'out/workflow-full-tests.json'
                test_report.unlink(missing_ok=True)
                result=self.command(sha,'tests',['python','-B',str(self.driver),
                                               '--report',str(test_report),
                                               *(f'--only={name}' for name in only or ()),*directories])
                report['stages']['tests']=result
                if test_report.is_file():
                    tests=json.loads(test_report.read_text(encoding='utf-8'))
                    report.update(test_files=tests['files'],tests=tests['tests'],skipped=tests['skipped'])
                    failures.extend(tests['failures'])
                    if result['returncode'] and not tests['failures']:
                        failures.append({'id':'stage:tests','detail':result['log']})
                else: failures.append({'id':'stage:tests','detail':result['log']})
            else:
                report['stages']['tests']={'skipped':'验证策略未选测试目录','returncode':0,'seconds':0,'log':''}
                report.update(test_files=[],tests=0,skipped=[])
            # unittest gates are included in the discovered suite; other gates still run.
            modules={p[:-3].replace('/','.') for p in report.get('test_files',[])}
            for number, cmd in enumerate([] if only is not None else plan['gates']):
                if 'unittest' in cmd and set(cmd[cmd.index('unittest')+1:]) and set(cmd[cmd.index('unittest')+1:]) <= modules:
                    continue
                label=f'gate-{number}'
                result=self.command(sha,label,cmd);report['stages'][label]=result
                if result['returncode']: failures.append({'id':'stage:'+label.split('-')[0]+':'+subprocess.list2cmdline(cmd),'detail':result['log']})
            for number, cmd in enumerate([] if only is not None else build):
                label=f'build-{number}'
                result=self.command(sha,label,cmd);report['stages'][label]=result
                if result['returncode']: failures.append({'id':'stage:'+label.split('-')[0]+':'+subprocess.list2cmdline(cmd),'detail':result['log']})
            changed=git(self.workspace,'status','--porcelain','--untracked-files=all').stdout
            if changed:
                # 列出改动的路径：只说「被改动」的票据无从归因。
                lines=changed.splitlines()
                more=f'\n（另有 {len(lines)-40} 条）' if len(lines)>40 else ''
                failures.append({'id':'stage:workspace-changed','detail':'验证期间工作树被改动，不发布生成提交：\n'+'\n'.join(lines[:40])+more})
                generation_ok=False
            if generation_ok:
                generated=report['generated_commit']
                if generated == sha: report['publication']='该源提交的派生物已一致'
                elif probe or not self.publish: report['publication']='仅报告模式；生成提交保存在验证副本'
                elif (tip:=self.current()) != sha:
                    report['publication']=self.publish_onto(tip, generated)
                else:
                    pushed=git(self.workspace,'push','origin',f'{generated}:refs/heads/main',check=False)
                    if pushed.returncode:
                        report['publication']='推送被拒绝，未覆盖 main'
                        if self.current()==sha: failures.append({'id':'stage:publish','detail':pushed.stderr})
                    else: report['publication']='已快进补齐 main'
        except Exception as exc:
            failures.append({'id':'stage:infrastructure','detail':str(exc)})
        observed=time.time()
        unique={f['id']:f for f in failures}
        if probe:
            report['failures']=list(unique.values())
            write_state(self.directory/'runs'/f'{sha}-probe.json',report)
            print(f'二分套件 {sha}：红 {len(unique)}',flush=True)
            return report
        # **先把 tip 这一份发出去，再去归因。**
        #
        # tip 的红名单到这一行就已经算完了，而归因要跑二分探针——每个探针是一次
        # 完整的生成＋全量测试＋打包，几十分钟一个（2026-09-12 实测 80 分钟）。
        # 把发布压在归因后面，等于让「当前 main 是什么样」这个信号迟到几个小时，
        # 而外面看到的是 `已验到 <很久以前那个提交>`：**红不阻断合并的前提是有网兜着，
        # 网的信号迟到几小时，那条规矩就名存实亡。**
        #
        # **归因是给红加来源，不是给红本身把关**——所以它可以慢，可以跨轮补，
        # 但不该拦着当前信号出门。落盘一份不带归因的，随后再用带归因的覆盖。
        # 中途崩了也没关系：那时 `last` 已经指向真验过的 tip，报告是真的，只是缺来源。
        baseline=self.index.get('last')
        # **票据、首次观察时刻、趋势都不依赖归因**，先算齐——
        # 预发布那一份必须和最终那一份**形状一样**，否则消费端会在缺键上炸：
        # `show_report` 直接取 `report['delta']` 与 `item['ticket']`，`claim` 也按票据找。
        # **发一份缺键的报告比晚发更糟**：它让整个收件箱打不开，
        # 而收件箱是「开工第一件事」。2026-09-13 我就是这么把 `inbox list` 弄挂的。
        for item in unique.values():
            prior=previous.get(item['id'])
            item['first_seen']=prior['first_seen'] if prior else observed
            item['introduced_by']=prior.get('introduced_by') if prior else None
            item['incoming']=prior.get('incoming',[]) if prior else []
            if prior and prior.get('attribution_error'): item['attribution_error']=prior['attribution_error']
            if prior and prior.get('attribution_window'): item['attribution_window']=prior['attribution_window']
            item['ticket']=prior['ticket'] if prior else hashlib.sha256((sha+'\n'+item['id']).encode()).hexdigest()[:16]
        report['failures']=list(unique.values())
        report['delta']=len(unique)-len(previous) if contiguous else None
        report['covered_from']=old['commit'] if contiguous else None
        report['covered_count']=len(first_parent_range(self.workspace,report['covered_from'],sha) or [])
        report['finished']=time.time();report['seconds']=report['finished']-started
        report['attribution']='二分进行中：这一份是刚验完的 tip 结果，归因随后覆盖'
        write_state(self.directory/'runs'/f'{sha}.json',report)
        self.index['last']=sha
        self.save()
        work=self.attribution_work(baseline,sha,unique,previous) if contiguous else []
        try:
            introductions=self.bisect_failures(work,self.budget(report)) if work else {}
        finally:
            if work:
                git(self.workspace,'checkout','--detach','--force',report['generated_commit'] or sha)
        report.pop('attribution',None)   # 归因跑完了，这一份不再是「进行中」
        for item in unique.values():
            if item.get('introduced_by') is None:
                item['introduced_by']=introductions.get(item['id'])
            if item.get('attribution_error'): item['detail']+='\n'+item['attribution_error']
            introduced=item['introduced_by']
            if introduced and not item['incoming']:
                item['incoming']=git(self.workspace,'show','-s','--format=%P',introduced).stdout.split()[1:]
            ticket=self.directory/'findings'/f"{item['ticket']}.md"
            if not ticket.exists():
                ticket.write_text(self.finding(item, sha), encoding='utf-8')
        for name, prior in previous.items():
            if name not in unique:
                ticket=self.directory/'findings'/f"{prior['ticket']}.md"
                if ticket.is_file():
                    text=ticket.read_text(encoding='utf-8').replace('| 状态 | 待派 |','| 状态 | 已恢复 |').replace('| 状态 | 已认领 |','| 状态 | 已恢复 |')
                    ticket.write_text(text+f'\n恢复证据：{sha} 不再报告此失败。\n',encoding='utf-8')
        report['failures']=list(unique.values());report['delta']=len(unique)-len(previous) if contiguous else None
        report['finished']=time.time();report['seconds']=report['finished']-started
        self.index['last']=sha
        if not contiguous: self.index.pop('last_green',None)
        report['covered_from']=old['commit'] if contiguous else None
        report['covered_count']=len(first_parent_range(self.workspace,report['covered_from'],sha) or [])
        write_state(self.directory/'runs'/f'{sha}.json',report)
        if report['publication']=='已快进补齐 main':
            # These exact generated bytes were just tested. Do not regenerate our own commit forever.
            alias=dict(report,commit=report['generated_commit'])
            write_state(self.directory/'runs'/f"{alias['commit']}.json",alias)
            self.index['last']=alias['commit']
        if report['publication'] in ('已快进补齐 main','该源提交的派生物已一致'):
            # main 上这个提交的派生物与源一致，之后的生成判断以它为基准。
            self.index['last_consistent']=self.index['last']
        if not unique: self.index['last_green']=self.index['last']
        self.index.pop('active',None);self.index.pop('error',None);self.save()
        ring(self.remote,f"{sha} red={len(unique)} delta={report['delta']}; run shiftboss.inbox list")
        print(f"验证 {sha[:12]}：红 {len(unique)}，{report['seconds']:.3f}s；{report['publication']}",flush=True)
        return report

    def finding(self, item: dict, sha: str) -> str:
        """一条红的筛查报告。**这不是任务书**——任务书由派活方写。

        这里只放派活方算起来贵、验证器算得出的那几样：引入提交动了哪些路径、
        哪些在办任务书写着这些路径、同一失败以前有没有票据、这条红稳不稳定。
        **不给结论、不排优先级、不判值不值得修**——那些要全局，验证器没有。
        """
        introduced=item.get('introduced_by')
        paths=[]
        if introduced:
            changed=git(self.workspace,'show','--name-only','--format=','--first-parent',
                        introduced,check=False)
            paths=[line for line in changed.stdout.split('\n') if line.strip()]
        owners=taskbooks_touching(self.remote,paths[:40]) if paths else []
        same=[q.stem for q in (self.directory/'findings').glob('*.md')
              if q.stem!=item['ticket'] and item['id'] in q.read_text(encoding='utf-8')]
        claim=read_state(self.directory/'claims.json').get(item['ticket'])
        rows=[f"| 状态 | {'已认领' if claim else '待派'} |",
              f"| 负责 | {claim['taskbook']+' / '+claim['session_id'] if claim else '派活方'} |",
              f"| 首次观察提交 | {sha} |",
              f"| 引入提交 | {introduced or '基线已红，起点未知'} |",
              f"| 稳定性 | {flaky_history(self.directory,item['id'])} |",
              f"| 同一失败的旧票据 | {'、'.join(same) if same else '无'} |",
              f"| 提到这些路径的在办任务书 | {'、'.join(owners) if owners else '没有匹配上，可能是没人认领的地方'} |"]
        body=[f"# 后台验证筛查：{item['id']}", "", "| 项 | 值 |", "| --- | --- |", *rows, ""]
        if paths:
            body += ["引入提交改到的路径：", ""] + [f"- `{x}`" for x in paths[:40]]
            if len(paths)>40: body.append(f"- …共 {len(paths)} 条")
            body.append("")
        body += [item['detail'], "",
                 "**这份是给派活方读的筛查，不是任务书。** 要不要开线、并进哪条线、"
                 "先做哪个，由派活方定；开了线再 `shiftboss.verify claim` 关联。"]
        return "\n".join(body)+"\n"

    def watch(self, once=False, interval=2.0):
        lock=self.directory/'worker.lock'
        try:
            handle=lock.open('x',encoding='utf-8')
        except FileExistsError:
            raise ValueError(f'已有验证进程锁 {lock}；确认其 PID 已结束后才能删除')
        with handle: handle.write(str(os.getpid()))
        try:
            self.prepare()
            (self.directory/'stop').unlink(missing_ok=True)
            self.index.pop('error',None);self.save()
            while not (self.directory/'stop').exists():
                current=self.current(); last=self.index.get('last')
                if current != last:
                    contiguous=first_parent_range(self.workspace,last,current) is not None
                    self.run(current,contiguous)
                    if once: break
                elif once: break
                time.sleep(interval)
        except Exception as exc:
            self.index['error']=str(exc);self.save();ring(self.remote,'worker failed; run shiftboss.inbox list')
            raise
        finally:
            lock.unlink(missing_ok=True)


def stop_now(remote: Path, workspace: Path) -> int:
    """立即停：**结束整棵进程树**。只杀主进程会留下它起的生成子进程，继续占着日志与验证工作区；
    被结束的 git 留下的 `index.lock` 会让下一次启动失败，一并删掉。"""
    import psutil
    lock=state_dir(remote)/'worker.lock'
    pid=lock.read_text(encoding='utf-8').strip() if lock.exists() else ''
    if pid.isdigit() and psutil.pid_exists(int(pid)):
        root=psutil.Process(int(pid))
        tree=root.children(recursive=True)+[root]
        for proc in tree:
            try: proc.kill()
            except psutil.NoSuchProcess: pass
        psutil.wait_procs(tree,timeout=10)
        print(f'已结束常驻验证 PID {pid} 及其 {len(tree)-1} 个子进程')
    else:
        print('常驻验证没在跑')
    lock.unlink(missing_ok=True)
    (workspace/'.git'/'index.lock').unlink(missing_ok=True);return 0


def start(repo: Path, workspace: Path, publish: bool) -> int:
    """以独立后台进程起 `watch`，工作目录为项目根。"""
    import psutil
    from shiftboss import native
    lock=state_dir(origin(repo))/'worker.lock'
    if lock.exists():
        pid=lock.read_text(encoding='utf-8').strip()
        if pid.isdigit() and psutil.pid_exists(int(pid)):
            print(f'常驻验证已在跑：PID {pid}；要换代码先 stop 或结束该进程');return 1
        print(f'锁 {lock} 记的 PID {pid or "无"} 已结束，删锁后再起');lock.unlink()
    argv=[sys.executable,'-B','-m','shiftboss.verify','watch','--repo',str(repo),
          '--workspace',str(workspace.resolve())]+(['--publish'] if publish else [])
    print(f'常驻验证已起：PID {native.spawn(argv,str(repo))}');return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['watch','start','status','claim','stop'])
    parser.add_argument('ticket',nargs='?');parser.add_argument('taskbook',nargs='?')
    parser.add_argument('--repo',type=Path,default=ROOT)
    parser.add_argument('--workspace',type=Path)
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--now',action='store_true',help='stop：不等当前提交验完，立即结束整棵进程树并删锁')
    parser.add_argument('--interval',type=float,default=2.0)
    args=parser.parse_args(argv)
    remote=origin(args.repo.resolve())
    if args.command=='status': print(show_report(remote));return 0
    if args.command=='claim':
        if not args.ticket or not args.taskbook: parser.error('claim 需要 ticket taskbook')
        claim(remote,args.ticket,args.taskbook);return 0
    if args.command=='stop' and args.now: return stop_now(remote,args.workspace or config.copies_dir(remote)/'verify')
    if args.command=='stop':
        state_dir(remote).mkdir(parents=True,exist_ok=True)
        (state_dir(remote)/'stop').touch();print('已请求在当前提交验证结束后停止');return 0
    # 验证工作区缺省放在副本目录下的 verify/，与执行方副本同处一地。
    args.workspace=args.workspace or config.copies_dir(remote)/'verify'
    if args.interval<=0: parser.error('interval 必须为正数')
    if args.command=='start': return start(args.repo.resolve(),args.workspace,args.publish)
    verifier=Verifier(args.repo,args.workspace,args.publish)
    verifier.watch(args.once,args.interval)
    return 0

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
