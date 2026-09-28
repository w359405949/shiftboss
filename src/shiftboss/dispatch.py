#!/usr/bin/env python3
"""派一份任务书给执行方。**派活一律走这里，不要手敲执行方的命令行。**

执行方按轮选：`--executor <名>` 走 `shiftboss.workers` 里对应的插件，缺省由 `shiftboss.toml` 的 `[workers] default` 给（缺省 codex）；
表头副本不在该执行方的池里时，换到该池的空闲副本并随本轮提交写回表头（上一轮会话未关或活未推完则拒绝）。
各执行方配额、副本分开；不能 `queue` 的执行方，会话停在等输入时关掉再带交代恢复，在干活时拒派。
台账每次派出记一条轮次、执行方与实际模型，模型本身由用户在各自配置里指定。

手敲的问题不是麻烦，是**信息只活在提示词里**：副本路径、分支名、每份都一样的
那几条约束，敲一次丢一次，换个会话或换个派活方就得重新拼。这里把它们全部
从任务书表头读出来——表头是唯一事实，敲错了对不上会直接报错而不是派歪。

**派 TUI，不是非交互执行**：一份活一个以任务书命名的终端标签页。

**多轮与返工复用同一个会话**，会话号记在控制目录的 `sessions.json`。
新一轮只在执行方交回停下后派：停着的会话关掉、`resume <id>` 带着历史开新一轮 →
**打回满 3 次就换新会话**，那时候历史已经变成负担。执行方还在做本轮时拒派；
往活会话投消息（能 queue 的执行方）只用于 `--recover` 接回意外中断的本轮。
核完一份就 `--close`，别让空转会话在副本上堆着。

**复用会话有代价，别忘了**：它记得的是**自己上一轮**的数字，而 `main` 这期间会变。
2026-09-01 有一轮报「计数保持 1985」——以它自己分支的起点看没错，可 `main` 已经
到 1992。所以 **`-m` 里的基准数字必须现算后写进去**，不能指望它记得对。

**模型不指定**，用 `~/.codex/config.toml` 里的默认值；要换模型改那份配置，
不要在这里加 `-m`——写在这里的会静默盖掉配置，两边不一致时很难发现。

**权限全开**：`-s danger-full-access -a never`。`-a never` 不能省——`exec` 子命令
根本没有 `-a`（非交互就是从不询问），而 TUI 默认会问，不写就停在审批框前面等人点。

**交付模式（单轮 / 连做 N / 自走）只写在任务书表头**，执行方从表头读，
停止条件在任务书合同「派发与交付」一节。这里没有对应的命令行参数，提示词里也不提。

用法：
    shiftboss dispatch <任务书名> [-m "这一轮的额外交代"]
    shiftboss dispatch <任务书名> --dry-run

`--dry-run` 打印将要执行的一切但不派，用来核对。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from shiftboss import native
from shiftboss.paths import PLANS_DIR, same_dir
from shiftboss.workers import WORKERS, default as default_worker

EXECUTORS = tuple(WORKERS)
# `shiftboss/DONE.md` 的列名。
DONE_LEVELS = ("原型", "成形", "堆叠", "收敛", "维护")

REPO = project_root()
HEADER_CELL = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
# **打回三次还没解决就换新会话。** 复用会话省的是重读，代价是它带着自己的历史；
# 在一个错里打转时，那段历史正是让它转不出来的东西。
MAX_ATTEMPTS = 3

# **提示词只说一件事：按工作流去读哪一份。模式、停止条件、交代一个字都不写。**
#
# 用户 2026-09-09 定的：「都必须走任务书内部条款描述，而不能通过命令行参数去提供，
# 命令行参数本身越简单越好……对于流程的固定内容，不要在任务书或参数内反复提示，
# 而是应该写进工作流基础文档……理论上只需要告诉他根据工作流的指示去读那份任务书，
# 并根据照着任务书列出的要求来完成任务即可，其他的表述都多余。」
#
# **为什么**：写在提示词里的话不进 git——追溯不到，换个会话就没了，
# 而且每加一句都在和 `README.md` 第 4 节争「哪一份才是规矩」。
# 核对位置、同步 `main`、先同步再读、已交的不许丢、卡住标待议、连做的四条停止条件，
# 全部在第 4 节；`AGENTS.md` 已经把执行方指过去了。**这里再说一遍只会让两处漂移。**
#
# 这条不是新发明：`RECOVER_PROMPT` 早就是一个「继续」，用户 2026-09-05 定死的，
# 理由一样——它自己看得见现场，复述只会把它推去重做已经做完的那半。
#
# **这一轮的交代走 `-m` / `-F`**：写进任务书的轮次记录小节并提交推送，
# 执行方从任务书里读到。第 4 节已经写明「轮次记录小节末尾有交代就照读」，
# 所以这里也不需要提示它去看。
STANDING_PROMPT = "开始新任务：按工作流读 {taskbook}。"

# **新一轮，不是「继续」。** 只在执行方交回停下之后才派：它带着历史回来开新的一轮，
# 按第 4.1 节同步并读轮次记录末条。执行方还在做本轮时一律拒派——追加的要求只进任务书，
# 等它交回再作为新一轮；严重影响本轮的，先 `--close` 停下，再重开会话或 `--fresh`。
NEW_ROUND_PROMPT = "新一轮：按工作流读 {taskbook}。"

# **交付模式不进提示词。** 曾经有过「连做 N 个 pass」和「自走：没有上限……」两句变体，
# 结果是模式同时活在表头与提示词两处：表头改成自走了，提示词还在说连做 15，
# 执行方做满就停。**表头是唯一一处**，执行方按第 4 节去读它。
# **提示词只指路，规则只写在文档里**（何时停见 README 2.4，轮次交代见 4.1）；提示词里多一句规则就多一处要同步。

# `--recover`：断电或崩溃之后把执行方接回来。**这段话要它先看清现场再动手**，
# 因为它的工作树里可能还留着自己上一轮没提交的东西，而它自己不知道断过电。
# **恢复只说「继续」。** `resume` 带着完整历史回来：它自己看得见工作树、看得见
# 做到哪一步，也会自己查断电写坏的文件。把这些再复述一遍不会让它更准，
# 反而容易把它推去重做已经做完的那半。2026-09-05 断电后由用户定死这条。
# **「继续」只用于意外中断**（断电、崩溃、额度用尽等）后接回本轮，不用于派新一轮或追加要求。
RECOVER_PROMPT = "继续"


def start_fresh(entry: dict, *, forced: bool, burned: bool,
                own_wip: bool = False) -> tuple[bool, str]:
    """要不要开新会话，以及为什么。**`closed` 是其中一条，别漏。**

    `--close` 的意思是这条线这一轮收摊了；合并成功也会自动 close。
    **收摊之后再派就是新活，必须开新会话**——复用那个会话等于把上一份合同、
    已经作废的裁定、十几轮的交回一起塞进上下文：既是噪音，也是每轮都要重付的额度。
    2026-09-09 并线后重派 `battle-message-window-in-scene`，resume 回一个背着 14 轮、
    累计输入 700 多万 token 的会话，就是漏了这一条。

    **该记住的东西不在会话里，在任务书里**：合同、轮次记录、交回块，
    新会话第 0 步就会读到。会话历史只是它自己上一轮的草稿。

    `--recover` 不走这里：断电恢复要的正是那段历史。
    """
    if forced:
        return True, "--fresh"
    if burned:
        return True, "返工满 3 次"
    if not entry.get("session_id"):
        return True, "台账里没有会话号"
    # 本轮中途 `--close` 下来改要求时，脏树是它自己没做完的活，历史正是认得这棵树的那段。
    if entry.get("closed") and not own_wip:
        return True, "上一轮已收摊（--close 或合并时自动关）"
    return False, ""


def header(text: str) -> dict[str, str]:
    """存量单文件布局的表头。**新布局读 `meta.json`，走 `Taskbook.meta()`。**

    留着这个名字是因为 `merge` 还在按它调；实现只此一份，在 `shiftboss.taskbook`。
    """
    from shiftboss.taskbook import parse_header
    return parse_header(text)


def keeps_going(mode: str | None) -> bool:
    """表头「交付模式」说的这条线**推完之后还会不会自己往下做**。

    **判「会不会继续」，不判某个词。** 会继续的模式不止一个（`连做 N`、`自走`），
    而以后还会有；按词判的那一版每加一个新模式名就漏一次，
    而漏掉的表现各不相同。**所以反过来判**：只有存量任务书的「单轮」不继续；
    新建的一律 `自走`（README 2.4）。归不归档不看它，由 `merge --done` 定。
    """
    return not str(mode or "单轮").startswith("单轮")


def executor_of(value: str | None) -> str:
    """任务书 `执行方` 与台账 `executor` 的归一：以某个执行方名开头的归它，其余（含缺省）归缺省执行方。"""
    text = str(value or "").strip().lower()
    return next((name for name in WORKERS if text.startswith(name)), default_worker())


LAUNCH_WAIT = 60


def launch_tui(copy_win: str, prompt: str, title: str, *,
               resume_id: str | None = None, executor: str | None = None) -> bool:
    """一份活一个以任务书命名的终端标签页，几份并行也认得出谁是谁。

    **起没起来以副本上有没有执行方进程为准**：`wt.exe` 开完标签页就返回 0，TUI 随即退出也照样成功，
    曾因此多条线「已派出」却从没开工。等不到进程返回 False，调用方不得记成已派出；不自动重拉
    （起不来多是网络问题，重拉无益）。
    """
    worker = WORKERS[executor or default_worker()]
    native.launch(worker.tui_command(copy_win, prompt, resume_id=resume_id), copy_win, title)
    for _ in range(LAUNCH_WAIT):
        if tui_alive(copy_win):
            return True
        time.sleep(1)
    return False


def origin_bare() -> Path:
    from shiftboss.inbox import origin_git_dir
    return origin_git_dir(REPO)


def sessions_path() -> Path:
    """会话台账放控制目录，**不放 out/**——那个目录随时可整根删除。
    和 inbox 的 ack 游标同一个地方，同一个道理：这是跨会话要活下来的状态。
    """
    from shiftboss.config import control_dir
    return control_dir(origin_bare()) / "sessions.json"


def load_sessions() -> dict:
    # 读写都走 shiftboss.state 那一对：原子改名写、读不动就报错退出。
    # **不要在这里另写一份**——两份状态文件用两套写法，坏起来只坏一份，更难发现。
    from shiftboss.state import read_state
    return read_state(sessions_path())


def save_sessions(data: dict) -> None:
    from shiftboss.state import write_state
    write_state(sessions_path(), data)


def newest_session(copy_win: str, since: float, executor: str | None = None,
                   previous: str | None = None, previous_steps: int | None = None) -> str | None:
    """`since` 之后这个副本上在用的会话号。派完立刻记下来，好让下一轮投得进去；判据见各执行方插件。"""
    worker = WORKERS[executor or default_worker()]
    return worker.newest_session(copy_win, since, previous, previous_steps)


def tui_alive(copy_win: str) -> bool:
    return bool(copy_processes(copy_win))


def session_model(entry: dict) -> str | None:
    """这条线会话里实际使用的模型；读不到返回 None。"""
    return WORKERS[executor_of(entry.get("executor"))].model_of(entry.get("session_id"))


def executor_processes():
    """Read live OS evidence. psutil supplies Windows process cwd as well as argv.

    Missing dependency/access is an error, never evidence of an empty copy.
    """
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("无法判活：需要 psutil（进程 cwd 读取），拒绝派活") from exc
    PROCESS_NAMES = {name for worker in WORKERS.values() for name in worker.PROCESS_NAMES}
    for process in psutil.process_iter():
        try:
            if process.name().lower() in PROCESS_NAMES:
                yield process
        except psutil.NoSuchProcess:
            continue


def copy_processes(copy_win: str) -> list:
    import psutil
    found = []
    for process in executor_processes():
        try:
            argv = process.cmdline()
            # An explicit -C overrides the inherited OS cwd.
            directory = next((argv[i + 1] for i, arg in enumerate(argv[:-1])
                              if arg in ("-C", "--cd")), None)
            directory = directory or process.cwd()
            if same_dir(directory, copy_win):
                found.append(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError(f"无法确认执行方进程 PID {process.pid} 的副本；拒绝当作不在") from exc
    # 执行方工具自己拉起的执行方进程（如 Codex 浏览器工具下的 `codex app-server`）归它的祖先会话，不算另一个会话。
    pids = {p.pid for p in found}
    top = []
    for process in found:
        try:
            if not any(parent.pid in pids for parent in process.parents()):
                top.append(process)
        except psutil.NoSuchProcess:
            continue
    return top


def foreign_session_reason(copy_win: str, entry: dict) -> str | None:
    processes = copy_processes(copy_win)
    for process in processes:
        argv = process.cmdline()
        worker = next((w for w in WORKERS.values() if process.name().lower() in w.PROCESS_NAMES), None)
        configured = bool(worker and worker.configured(argv))
        identity = {"pid": process.pid, "created": process.create_time()}
        if (not configured or identity not in entry.get("processes", [])
                or not entry.get("session_id") or entry.get("closed")
                or not same_dir(entry.get("copy"), copy_win) or len(processes) > 1):
            return (f"拒绝：副本 {copy_win} 有外来或无法归属的活会话 PID {process.pid}；"
                    "先关掉那个标签页再来（也可对该任务书 --close），不会开第二个会话。")
    return None


def process_identities(copy_win: str) -> list[dict]:
    """Remember launch ownership; PID alone can be recycled by the OS."""
    return [{"pid": p.pid, "created": p.create_time()} for p in copy_processes(copy_win)]


def launched_identities(copy_win: str, seconds: int = 30) -> list[dict]:
    """**刚 `launch_tui` 完就要记凭据的，一律走这里，不要直接调上面那个。**

    `launch_tui` 交给 `wt new-tab` 之后立刻返回，codex.exe 是之后才起来的。
    紧接着快照，记下的就是一份空名单——而**台账里没有进程凭据的会话，
    `foreign_session_reason` 会当成外来进程**，下一次派活对着自己刚起的
    标签页报「有外来或无法归属的活会话」，只能先手工关掉才能继续。
    正常派活那条路要等 `newest_session` 才写台账，顺带躲过了这一段；
    恢复那条路没有那一步，所以这个等待得自己带着。
    """
    for _ in range(seconds):
        identities = process_identities(copy_win)
        if identities:
            return identities
        time.sleep(1)
    return []


def sandbox_warnings(ledger: dict) -> list[str]:
    """活着的线里权限不是全开的会话；判据见各执行方插件的 `sandbox_warning`。"""
    warnings = []
    for name, entry in ledger.items():
        if entry.get("closed") or not entry.get("session_id"):
            continue
        sid = entry["session_id"]
        problem = WORKERS[executor_of(entry.get("executor"))].sandbox_warning(sid)
        if problem:
            warnings.append(f"沙箱告警：{name} 副本 {entry.get('copy', '未确认')}，会话 {sid}，{problem}")
    return warnings


def read_quota(executor: str = "codex") -> int:
    """**同时最多派几条线**。**0 ＝ 不给这个执行方分配**；没有这份文件同样是不分配。

    **台账本身就是「现在有几条活着」**——派活时写入、`--close` 时删除，
    所以不需要另立一张占位表，也不需要在状态表上加标记。
    2026-09-02 我第一版做了 `lines` 列表 ＋ `▶` 标记，是把简单需求做复杂了。

    **这是个棘轮：只拦新派，不动正在跑的。** 手上那条做完、核过、`--close`
    之后位置自然腾出来。
    """
    from shiftboss.state import read_quota as read
    return read(origin_bare(), executor)


def write_quota(value: int, *, check: bool = False, executor: str = "codex") -> bool:
    """`shiftboss dispatch --quota N` 会先复用 bootstrap 补齐缺的副本
      （克隆、本机配置、提交守卫），全部可用之后才写配额。
    """
    from shiftboss.bootstrap import configure_copies
    return configure_copies(REPO, origin_bare(), value, check, executor)


def active_lines(ledger: dict, executor: str | None = None) -> list[str]:
    """还占着额度的那几条。**`--close` 过的不算**——它留在台账里只为保住会话号。

    给了 `executor` 就只数这个执行方的。
    """
    return sorted(n for n, e in ledger.items() if not e.get("closed")
                  and (executor is None or executor_of(e.get("executor")) == executor))


def capacity(ledger: dict) -> list[tuple[str, int, int]]:
    """各执行方的 (名字, 在跑条数, 配额)；缺省执行方总是列出，其余有配额或在跑才列。"""
    rows = []
    for executor in EXECUTORS:
        active, cap = len(active_lines(ledger, executor)), read_quota(executor)
        if executor == default_worker() or active or cap:
            rows.append((executor, active, cap))
    return rows


def over_quota(taskbook: str, ledger: dict, executor: str = "codex") -> str | None:
    """超额就返回**为什么**，没超返回 None。

    配额只拦新派，降低它不打断正在跑的线；按执行方各算各的。
    """
    cap = read_quota(executor)
    active = active_lines(ledger, executor)
    # **配额 0 连已占位的线也不再派下一轮**：自走线从不 `--close`，
    # 下面那条豁免会让「撤掉这个执行方」永远不生效。已在干的那一轮不受影响，
    # `--recover` 不走这里。
    if cap <= 0:
        return (f"{executor} 没有分配额度（配额 {cap}），不派。\n"
                f"已在跑的线先 `--close`，再用 `--executor <别的执行方>` 派下一轮；"
                f"要用它就先分配：`shiftboss dispatch --quota N --executor {executor}`")
    # **正在跑的那条不受配额上限拦**：配额是棘轮，降低它不打断手上的活。
    if taskbook in active:
        return None
    if len(active) >= cap:
        held = "、".join(active)
        return (f"{executor} 同时最多 {cap} 条线，现在已有 {len(active)} 条：{held}\n"
                f"等它做完核过 `--close`，位置自然腾出来；"
                f"要改上限：`shiftboss dispatch --quota N --executor {executor}`")
    return None


def queue_into(session_id: str, message: str, copy_win: str = "",
               executor: str | None = None) -> bool:
    """往活着的会话里投一条（只供 `--recover` 接回意外中断的本轮），**返回是否真的投进去了**；判据见各执行方插件。"""
    alive = (lambda: tui_alive(copy_win)) if copy_win else None
    return WORKERS[executor or default_worker()].queue_into(session_id, message, alive)


def close_session(copy_win: str) -> bool:
    """关掉这个副本上的 TUI。**按副本找，不是按 session_id**——命令行里没有会话号。
    前提是一个副本同时只有一个活会话；这套流程本身就在维持这条。
    rollout 留在盘上，随时 `codex resume` 回得去。
    """
    import psutil
    processes = copy_processes(copy_win)
    for process in processes:
        try:
            for child in process.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
    return bool(processes)


def close_line(name: str, copy_win: str, ledger: dict) -> None:
    """Shared --close / merge semantics; preserve session identity and rollout."""
    entry = ledger.get(name) or {}
    holder = next((n for n, e in ledger.items() if n != name and not e.get("closed")
                   and same_dir(e.get("copy"), copy_win)), None)
    if holder:
        raise RuntimeError(f"不能关闭 {name}：副本已由 {holder} 占用")
    ok = close_session(copy_win)
    if entry:
        entry["closed"] = True
    save_sessions(ledger)
    print(f"{name} 的会话{'已关闭' if ok else '本来就没在跑'}，不再占额度；"
          f"保留会话 {entry.get('session_id', '无')} 和 rollout。")


def today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def now_iso() -> str:
    """派出的**时刻**，不只是日期。

    收件箱判「这条线是停着还是在干活」要拿它跟分支最后一次 push 比；
    只有日期比不出来（同一天派两轮是常态）。
    """
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def round_number(book) -> int:
    """下一轮的编号。**取已出现的最大轮次号 +1，不数还剩几条记录。**

    轮次记录会被压缩（README 3.7），**按条数算的编号在压缩之后会倒退**：
    第 53 轮压成 3 条，下一轮就变成「第 4 轮」，而分支上的提交、交回里的引用、
    验证报告里说的还是旧编号，从此两套编号并存且没人说得清哪套算数。
    **编号是这条线的坐标，不是这一节还剩多少字。**
    取最大值同时也免疫历史上重复或跳号的标题。
    """
    headings = markdown_headings(book.rounds_text())
    seen = [int(m[1]) for pos, stop, depth, title in headings
            if depth == book.round_level()
            and (m := re.match(r"第\s*(\d+)\s*轮(?:\D|$)", title))]
    # 全部压掉时没有轮次标题可取，上界只剩「已压掉的 N 轮记录（第 X–Y 轮…）」那一行。
    seen += [int(m[1]) for pos, stop, depth, title in headings
             if (m := re.search(r"已压掉.*第\s*\d+\s*[–—-]\s*(\d+)\s*轮", title))]
    return max(seen, default=0) + 1


ROUNDS_START = "<!-- workflow:rounds:start -->"
ROUNDS_END = "<!-- workflow:rounds:end -->"

# 一份任务书最多派多少轮，到了就拆。**规则在 README 3.4。**
ROUND_LIMIT = 20


def markdown_headings(text: str) -> list[tuple[int, int, int, str]]:
    """只读正文 ATX 标题；代码围栏里的示例不算章节或轮次。"""
    headings = []
    fence = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = ""
        elif match := re.match(r"^ {0,3}(`{3,}|~{3,})", line):
            fence = match[1]
        elif match := re.match(r"^(#{1,6})\s+(.+?)\s*$", line):
            headings.append((offset, offset + len(line), len(match[1]), match[2]))
        offset += len(line)
    return headings


def rounds_region(text: str) -> tuple[int, int, int]:
    """显式边界优先；无标记的存量任务书仅接受旧 4 节或一块内的 3 小节。

    标题措辞可改。改标记、章节编号或层级属于结构变更，无法唯一定位就拒派。
    """
    headings = markdown_headings(text)
    if ROUNDS_START in text or ROUNDS_END in text:
        if text.count(ROUNDS_START) != 1 or text.count(ROUNDS_END) != 1:
            raise ValueError("轮次记录标记必须各有且仅有一个")
        start = text.index(ROUNDS_START) + len(ROUNDS_START)
        end = text.index(ROUNDS_END)
        preceding = [h for h in headings if h[1] <= start - len(ROUNDS_START)]
        if start >= end or not preceding:
            raise ValueError("轮次记录标记顺序错误或缺少所属标题")
        level = preceding[-1][2]
        if any(depth <= level for pos, stop, depth, title in headings if start <= pos < end):
            raise ValueError("轮次记录标记跨越了所属章节")
        return start, end, level
    candidates = []
    parent = ""
    for pos, stop, level, title in headings:
        if level == 2:
            parent = title
        if (level == 2 and re.match(r"4[.．、](?:\s|$)", title)
                or level == 3 and re.match(r"一(?:\s|[、.．])", parent)
                and re.match(r"3[.．、](?:\s|$)", title)):
            end = next((p for p, s, depth, name in headings if p > pos and depth <= level), len(text))
            # 分块分隔线留在章节外，交代写在它前面。
            separator = re.search(r"\n---\s*\Z", text[stop:end])
            if separator:
                end = stop + separator.start()
            candidates.append((stop, end, level))
    if len(candidates) != 1:
        raise ValueError("无法唯一定位轮次记录；请在所属章节内添加 workflow:rounds:start/end 标记")
    return candidates[0]


def round_cap_due(book) -> str | None:
    """**轮次到 `ROUND_LIMIT` 就拒派，要求拆分。规则在 README 3.4。**

    轮次一多，既说不清还有没有有效产出，上下文也拖慢执行方；压缩记录只是缓解。
    到了上限说明目标定得太宽或已经分裂：拆成能各自做完的新任务书（表头 `来源` 指回这份），
    再 `shiftboss.merge --done` 收掉这份。
    """
    try:
        rounds = int(book.meta().get("轮次") or 0)
    except (TypeError, ValueError):
        rounds = 0
    if rounds < ROUND_LIMIT:
        return None
    return (f"拒绝：{book.name} 已派 {rounds} 轮，到上限 {ROUND_LIMIT}。\n"
            f"拆成能各自做完的新任务书（表头 `来源={book.name}`），"
            f"再 `shiftboss merge --done {book.name}` 收掉这份。")


def colliding_headings(message: str, level: int) -> list[str]:
    """交代正文里**会把轮次区那一节顶掉**的标题。

    轮次区归一个 `level` 级章节管，所以区内最浅只能到 `level + 1`
    （`dispatch` 自己生成的「第 N 轮」那行就是 `level + 1`）。
    正文里出现一个 `level` 级或更浅的标题，按 Markdown 的读法**就是结束了那一节**，
    于是 `rounds:end` 落在另一个章节里，`rounds_region` 从此拒绝整份任务书。

    **必须写之前拦，写完再查没有用**：`write_note` 是先算区间、再插入、再写盘，
    算区间时文件还是好的，**所以弄坏它的那一次派活一路成功**，
    报错要等下一次碰这份任务书——那时多半已经换了会话，
    只剩一句「轮次记录标记跨越了所属章节」，跟半天前那次派活对不上号。

    代码围栏里的 `#` 不算标题，`markdown_headings` 已经跳过了。
    """
    return [f"{'#' * depth} {title}" for pos, stop, depth, title
            in markdown_headings(message) if depth <= level]


# 交代与合同里不许出现的代码围栏语言。**贴源码几乎一定是在指定实现。**
SOURCE_FENCES = ("js", "javascript", "jsx", "ts", "typescript",
                 "py", "python", "c", "cpp", "asm", "diff", "patch")


def source_fences(message: str) -> list[str]:
    """交代正文里带源码语言标签的代码围栏。

    **甲方只给目标，不给细节**（README 2）。而越界最常见的形状不是「我建议你这么写」，
    是**核对时读到的那段代码正躺在手上，顺手贴进了下一轮交代**——
    贴下去比抽象回去省力。贴进去的那一刻，「怎么做」就从乙方手上收走了。

    **字节表、地址、发布数据的形状是事实，照旧可以写**：`text` / 无标签围栏不拦，
    拦的只有源码语言。这条不模糊，也几乎不误伤。
    """
    fenced, hits = "", []
    for line in message.splitlines():
        stripped = line.strip()
        if fenced:
            if stripped.startswith(fenced):
                fenced = ""
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = stripped[:3]
            tag = stripped[3:].strip().lower().split()[0] if stripped[3:].strip() else ""
            if tag in SOURCE_FENCES:
                hits.append(tag)
    return hits


def copy_pool(copy_win: str) -> str:
    """副本属于哪个执行方的池：目录名 `<执行方名><序号>`；认不出的归缺省执行方。"""
    match = re.fullmatch(r"(" + "|".join(map(re.escape, WORKERS)) + r")\d+", Path(copy_win).name)
    return match[1] if match else default_worker()


def switch_copy(executor: str, entry: dict, old_copy: str, branch: str, ledger: dict) -> str:
    """本轮换执行方时分到的空闲副本。**上一轮的活没全推上去、会话没关就拒绝**，免得半截活留在旧副本。"""
    def git(copy: str, *args: str) -> str:
        return subprocess.run(["git", "-C", copy, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout.strip()

    if entry.get("session_id") and not entry.get("closed"):
        raise ValueError(f"拒绝换执行方：上一轮会话 {entry['session_id']} 还开着，先 --close")
    if Path(old_copy).exists():
        if git(old_copy, "status", "--porcelain"):
            raise ValueError(f"拒绝换执行方：旧副本 {old_copy} 有未提交改动")
        git(old_copy, "fetch", "-q", "origin")
        if git(old_copy, "rev-parse", "--abbrev-ref", "HEAD") == branch and git(
                old_copy, "rev-list", "--count", f"origin/{branch}..HEAD") not in ("", "0"):
            raise ValueError(f"拒绝换执行方：旧副本 {old_copy} 上 {branch} 有未推送提交")
    return free_copy(executor, ledger)


def free_copy(executor: str, ledger: dict) -> str:
    """该执行方池里的空闲副本：台账没人占、工作树干净、上面没有进程。"""
    for candidate in copies_by_preference(executor):
        copy = str(candidate)
        held = any(not e.get("closed") and same_dir(e.get("copy"), copy) for e in ledger.values())
        clean = not subprocess.run(["git", "-C", copy, "status", "--porcelain"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace").stdout.strip()
        if not held and clean and not copy_processes(copy):
            return copy
    raise ValueError(f"拒绝：{executor} 没有空闲副本")


def copies_by_preference(executor: str) -> list[Path]:
    """挑副本的顺序：**配额内的先来，多出的垫后**。

    配额内的副本闲着时，活不能落到 `bootstrap` 明说「多出的、原样保留」的那几个上。
    """
    from shiftboss.bootstrap import copy_paths, existing_copies
    from shiftboss.state import read_quota
    bare = origin_bare()
    existing = existing_copies(bare, executor)
    quota = read_quota(bare, executor)
    if not quota:
        return existing
    inside = [p for p in copy_paths(bare, quota, executor) if p in existing]
    return inside + [p for p in existing if p not in inside]


def write_note(book, message: str, executor: str = "codex") -> None:
    """把这一轮的交代写进轮次记录。**布局的事全归 `shiftboss.taskbook`。**"""
    level = book.round_level()
    # **目录布局里 rounds.md 整份就是轮次区，没有外层章节可撞**，这条检查只对存量布局成立。
    if not book.is_dir and (collisions := colliding_headings(message, level - 1)):
        raise ValueError(
            f"派活拒绝：这一轮的交代里有 {len(collisions)} 个标题会和任务书的章节层级相撞。\n"
            f"轮次区归 {level - 1} 级章节管，区内最浅只能用 {level} 级；"
            f"下面这些写进去会把那一节顶掉，之后 {book.name} 就再也解析不了：\n  "
            + "\n  ".join(collisions)
            + f"\n改成 {'#' * level} 或粗体再来。")
    if fences := source_fences(message):
        raise ValueError(
            f"派活拒绝：这一轮的交代里有 {len(fences)} 段源码围栏（{'、'.join(sorted(set(fences)))}）。\n"
            "**甲方只给目标，不给细节。** 贴源码就是把「怎么做」从乙方手上收走，\n"
            "而它手上有现场、有更细的检查，甲方只有二手资料。\n"
            "把那几段删掉，改写成「要成立什么」与「算不算做到了」；\n"
            "字节表、地址、发布数据的形状是事实，用 ```text 或不带标签的围栏照写不误。")
    n = round_number(book)
    book.append_round(f"{'#' * level} 第 {n} 轮派出时的交代（{today()}，{executor}）", message)
    if book.is_dir:
        book.set_meta(轮次=n)


def publication_base() -> str:
    """Refuse to publish unrelated commits before changing the taskbook.

    派活写轮次记录小节之前，`dispatch` 要求本地 main 与现取的 origin/main 完全相同且工作树干净；
    存在未推送提交时列出提交并拒绝。
    """
    def git(*args):
        return subprocess.check_output(["git", "-C", str(REPO), *args], text=True,
                                       encoding="utf-8").strip()
    if git("symbolic-ref", "--short", "HEAD") != "main":
        raise ValueError("派活提交必须在 main 上")
    git("fetch", "--quiet", "origin", "main")
    base = git("rev-parse", "FETCH_HEAD")
    extra = git("log", "--format=%h %s", f"{base}..HEAD")
    if extra:
        raise ValueError("派活拒绝：main 有非本次派活的未推送提交：\n" + extra)
    if git("rev-parse", "HEAD") != base:
        raise ValueError("派活拒绝：本地 main 落后 origin/main，请先同步")
    if git("status", "--porcelain"):
        raise ValueError("派活拒绝：main 工作树或索引不干净，请先处理已有改动")
    return base


def publish_note(book, message: str, executor: str = "codex", copy: str = "") -> None:
    """
    只提交任务书路径，推送固定提交 ID；
    其唯一父节点必须是先前记录的远端提交，避免并发提交被夹带。推送失败保留本地提交待处理。
    **目录布局给的是目录**：`add`／`commit --only` 对目录一样成立，
    而这一次改的可能是 rounds.md 与 meta.json 两个文件。
    `copy` 非空时同一提交把表头「你的工作副本」改成本轮分到的副本。
    """
    base = publication_base()
    if message:
        write_note(book, message, executor)
    if copy:
        book.set_meta(你的工作副本=copy)
    rel = book.rel(REPO)
    subprocess.run(["git", "-C", str(REPO), "add", "--", rel], check=True)
    subprocess.run(["git", "-C", str(REPO), "commit", "--only", "-q", "-m",
                    f"plans: {book.name} 这一轮的交代", "--", rel], check=True)
    commit = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
    parents = subprocess.check_output(["git", "-C", str(REPO), "show", "-s", "--format=%P", commit], text=True).strip()
    if parents != base:
        raise ValueError("派活拒绝：提交期间 main 改变；保留本地提交，不推送")
    # Pin the one authorized commit; a concurrent local commit cannot ride along.
    subprocess.run(["git", "-C", str(REPO), "push", "-q", "origin",
                    f"{commit}:refs/heads/main"], check=True)
    what = "与".join(part for part, on in (("交代", message), ("副本", copy)) if on)
    print(f"这一轮的{what}已写进 {rel} 并推送 {commit[:12]}")


def merge_main_into(copy: Path, branch: str) -> str:
    """投提示词之前把 origin/main 合入副本的目标分支，返回一句结果。

    执行方读的是副本里的轮次记录，分支没合 origin/main 就读不到本轮交代。
    只合不重置：工作树不干净、不在目标分支、fetch 失败或合并冲突时不改副本，交给执行方自己合。
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(copy), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    # **fetch 排在所有检查之前**：它不碰工作树，而执行方按 README 4.1 要从
    # `origin/main` 读表头才知道自己该去哪条分支。副本不在目标分支时如果连 fetch 都跳过，
    # 它读到的 `origin/main` 是陈的，新开的线在那儿根本不存在——2026-09-21 实测一次
    # 执行方因此判定「工作区不属于该任务」而停工。
    if git("fetch", "-q", "origin").returncode:
        return "副本 fetch 失败，未合 origin/main，交给执行方自己合"
    if git("status", "--porcelain").stdout.strip():
        return "副本工作树不干净，已 fetch、未切分支未合 origin/main，交给执行方自己合"
    # **副本不在目标分支时由这里切，不指望执行方自己切。**
    # 执行方读的规则（`shiftboss/README.md`）和任务书都在它自己的工作树里，
    # 停在旧分支时读到的是旧规则、也看不到新开的任务书目录——2026-09-21 实测两条
    # 新线因此拒绝开工，改了 README 也够不到它们，因为它们读的就是旧那份。
    # 工作树干净才走到这里，`checkout -B` 不会抹掉任何未提交的东西；
    # `--recover` 在更早处就返回了，不经过这里（脏树正是它要救的）。
    if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != branch:
        base = (f"origin/{branch}"
                if git("rev-parse", "--verify", "--quiet", f"origin/{branch}").returncode == 0
                else "origin/main")
        if git("checkout", "-B", branch, base).returncode:
            return f"副本切到 {branch} 失败，已 fetch、未合 origin/main，交给执行方自己切"
        if base == "origin/main":
            return f"副本已从 origin/main 切到 {branch}（新分支，内容即 main）"
    if git("merge", "--no-edit", "-q", "origin/main").returncode:
        git("merge", "--abort")
        return "origin/main 合入副本冲突，已撤回，交给执行方自己合"
    return f"已把 origin/main 合入副本的 {branch}"


def main(argv: list[str] | None = None) -> int:
    """派一份任务书出去

    副本路径与分支**从任务书表头读**，不从命令行给；多轮与返工在执行方停下后**恢复原会话**开新一轮，打回满 3 次才换新；执行方还在做本轮时拒派；**副本被另一份没 `--close` 的任务书占着时直接拒派**
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("taskbook", nargs="?", help="任务书名，不带 .md")
    ap.add_argument("-m", "--message", default="",
                    help="这一轮独有的交代。**会写进任务书轮次记录小节并提交推送**，不进提示词。"
                         "**带反引号或反斜杠的内容不要走这里，用 -F**")
    ap.add_argument("-F", "--message-file", type=Path, default=None,
                    help="从文件读这一轮的交代。**内容里有反引号、反斜杠或 $ 就必须用它**")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rework", action="store_true",
                    help="这一轮是打回返工。**只有它会加返工计数**；满 3 次自动换新会话。")
    ap.add_argument("--fresh", action="store_true",
                    help="强制开新会话（丢掉历史），并把返工计数清零。")
    ap.add_argument("--recover", action="store_true",
                    help="断电/崩溃后恢复：resume 原会话，**不碰工作树**（脏的正是要救的）")
    ap.add_argument("--close", action="store_true",
                    help="核完了：关闭会话并标 closed；台账会话号和 rollout 留着，可 resume。")
    ap.add_argument("--quota", type=int, metavar="N",
                    help="设配额并自动补齐工作副本（0＝不分配，不新增副本）。"
                         "这是棘轮，只拦新派，不动正在跑的。")
    ap.add_argument("--executor", choices=EXECUTORS, default=default_worker(),
                    help="本轮的执行方（缺省 codex）；与表头副本不在同一池时自动换到该执行方的空闲副本。"
                         "也是 --quota 作用的执行方。--recover 沿用台账里的执行方。")
    args = ap.parse_args(argv)

    from shiftboss.state import require_dispatcher
    require_dispatcher()

    # 缺项目配置时各项静默取中性缺省，派出去的线会带着错的路径跑；配置由派活方生成（README 3.0）。
    from shiftboss import config
    if args.taskbook and not (args.close or args.recover) and (path := config.missing()):
        print(f"缺项目配置 {path}：先按 shiftboss/README.md 3.0 生成再派")
        return 2

    # **交代里带反引号就要走 `-F`。** 命令行是经 shell 的：没引起来的反引号是命令
    # 替换，`shiftboss.dispatch ... -m "…`mount_mask` 和 `equipped_mask` 是两件事…"`
    # 到这里已经变成「… 和  是两件事…」——**名字被吃掉，句子还读得像句子**，
    # 而这段话已经提交推送、执行方也已经读过了。2026-09-07 就这么发出去一次。
    # 这里拦不住已经被吃掉的（到手时早就没了），能做的是**给一条不经 shell 的路**，
    # 并在明显像被吃过时提醒一句。
    if args.message_file:
        if args.message:
            print("-m 和 -F 只能给一个")
            return 2
        args.message = args.message_file.read_text(encoding="utf-8")
    elif args.message and re.search(r"\S {2,}\S", args.message):
        # 只是提示，不是判据：被吃掉的地方会留下句中连空格。拦不准，但提一句很便宜。
        print("提醒：这段交代里有句中连续空格，像是反引号被 shell 吃掉过。"
              "内容带反引号/反斜杠/$ 时改用 -F <文件>，不要走 -m。")

    if args.quota is not None:
        try:
            live = active_lines(load_sessions(), args.executor)
            ready = write_quota(args.quota, check=args.dry_run, executor=args.executor)
            if args.dry_run:
                print("--dry-run，配额未修改。")
                return 0 if ready else 1
            cap = read_quota(args.executor)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            print(f"副本准备失败，配额未修改：{detail}")
            return 1
        print(f"{args.executor} 同时最多 {cap} 条线（0＝未分配）；现在有 {len(live)} 条："
              f"{'、'.join(sorted(live)) or '（无）'}")
        return 0
    if not args.taskbook:
        ap.error("要么给任务书名，要么用 --quota N")

    # **台账键一律用文件名本身，不带目录。** 归档之后要写 `archive/<名字>` 才找得到
    # 文件，可台账里那条仍然叫 `<名字>`——不归一化的话，`--close` 会杀掉 TUI 却把
    # 标记写到一个不存在的键上，那条就永远占着额度（2026-09-04 我自己踩了两次）。
    args.taskbook = Path(args.taskbook).name
    plans = REPO / PLANS_DIR
    # **归档目录也要找。** 一份活做完的顺序是「核对 → 合并 → 移进 archive → --close」，
    # 而移进 archive 之后 `shiftboss/plans/<名字>` 就不在原处了：`--close` 直接报「没有这份
    # 任务书」退 2，台账里那条永远占着额度，只剩手改台账一条路。
    # 2026-09-05 归档 autosave-scene-and-shop 时撞上。**先归档再收摊是正常顺序，
    # 不该要求人反过来做。**
    from shiftboss.taskbook import Taskbook
    book = Taskbook.find(REPO, args.taskbook)
    if book is None:
        print(f"没有这份任务书：{plans / args.taskbook}"
              f"（单文件与目录两种布局、`archive/` 里都没有）")
        return 2
    path = book.path
    cells = book.meta()
    copy_win = cells.get("你的工作副本", "")
    branch = cells.get("分支", "")
    status = cells.get("调度状态", "")
    # 表头没写副本：首派时从执行方的池里分一个空闲的，随本轮交代一并写回表头。
    assigned_copy = ""
    if not copy_win and not args.close and not args.recover:
        try:
            copy_win = assigned_copy = free_copy(args.executor, load_sessions())
        except ValueError as exc:
            print(exc)
            return 2
    if not copy_win or not branch:
        print("任务书表头缺「你的工作副本」或「分支」，补齐再派")
        return 2
    copy = Path(copy_win)
    if not copy.exists():
        print(f"表头写的工作副本不存在：{copy}")
        return 2
    # **`--close` 不受「已归档」拦。** 归档恰恰是最该收摊的时候：不放行的话，
    # 台账里那条一直占着额度，而 `--close` 又够不着它，只剩手改台账一条路。
    if status in ("已归档", "已作废") and not args.close:
        print(f"这份任务书状态是 `{status}`，不该再派")
        return 2
    # **状态格说它不该派，就不派。** 表头是执行方判断处境的依据，也是我打开文件
    # 第一眼看到的东西；它和实际在跑对不上，下一次就会有人以为这条线没派出去。
    # 2026-09-05 实测：`monster-attack-effects` 已经派出去 21 分钟，表头还写着「待议」，
    # 用户打开文件就以为它还没派。**拦在这里而不是靠记得改**——真要派就先把格子改对。
    if not args.close and any(
            status.startswith(word) for word in
            ("编写中", "核对中", "待议", "待定夺")):
        print(f"表头说它现在不该派（状态 `{status}`）。\n"
              f"真要派就先把状态格改对——表头是执行方判断处境的依据，"
              f"也是下一个人第一眼看到的东西。")
        return 2

    # 执行方按这一格去 DONE.md 查段内能做什么；缺了它就只能自己推断，所以拦在派活前。
    if not args.close and cells.get("完成标准") not in DONE_LEVELS:
        print(f"表头「完成标准」是 `{cells.get('完成标准', '（缺）')}`，"
              f"须为 {'／'.join(DONE_LEVELS)} 之一（见 shiftboss/DONE.md）；"
              f"改：shiftboss taskbook set {args.taskbook} --field 完成标准=<列名>")
        return 2

    ledger = load_sessions()
    entry = ledger.get(args.taskbook) or {}
    if args.close:
        if args.dry_run:
            print("--dry-run，没有关闭。")
            return 0
        close_line(args.taskbook, copy_win, ledger)
        return 0

    # 执行方按轮定：恢复沿用台账里那一轮的，其余取本次参数；副本不在该执行方的池里就换一个空闲的。
    executor = executor_of(entry.get("executor")) if args.recover else args.executor
    new_copy = assigned_copy
    if not args.recover and copy_pool(copy_win) != executor:
        try:
            new_copy = switch_copy(executor, entry, copy_win, branch, ledger)
        except (ValueError, RuntimeError) as exc:
            print(exc)
            return 2
        print(f"本轮执行方 {executor}：副本由 {copy.name} 换为 {Path(new_copy).name}")
        copy_win, copy = new_copy, Path(new_copy)

    if args.recover:
        for warning in sandbox_warnings({args.taskbook: entry}):
            print(warning)
    try:
        denied = foreign_session_reason(copy_win, entry)
    except (RuntimeError, OSError, ImportError) as exc:
        denied = f"拒绝：无法判活：{exc}"
    if denied:
        print(denied)
        return 2

    holder = next((name for name, other in ledger.items()
                   if name != args.taskbook and not other.get("closed")
                   and same_dir(other.get("copy"), copy_win)), None)
    if holder:
        print(f"拒绝：{copy_win} 还被 {holder} 占着；先关闭那条线再来。")
        return 2

    if args.recover:
        # **断电/崩溃之后的恢复。跟正常派活反着来，两条都不能省：**
        # 1. **不查工作树干不干净**——脏的正是要救的东西（执行方做到一半的活）。
        # 2. **一个字都不碰工作树**：正常路径那句 `checkout -B <分支> <基>` 会把
        #    没提交的改动直接抹掉。2026-09-01 断电时 codex3 就有 10 个文件没提交。
        old_id = entry.get("session_id")
        if not old_id:
            print(f"台账里没有 {args.taskbook} 的会话号，恢复不了；只能重新派一轮。")
            return 2
        dirty = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        print(f"恢复 {args.taskbook}：会话 {old_id}，"
              f"工作树{'有 ' + str(len(dirty.splitlines())) + ' 个未提交改动（会交给它自己收）' if dirty else '干净'}")
        if args.dry_run:
            print("--dry-run，没有恢复。")
            return 0
        # **TUI 还活着就走 `queue`，不要 `resume`。**
        # `resume` 是给「会话已经死了」那一档用的；会话还活着时它开出来的新标签页
        # 接不上那个仍在跑的 TUI，消息就此消失——从外面看是「已恢复」，实际什么都没发生。
        # 2026-09-07 我这么让一条线在副本里躺了快两小时：活干完了、提交在本地，
        # 就等我回一句话，而那句话根本没送到。正常派活那条路早有三段阶梯，
        # **恢复这条路当初漏了它**。恢复自己那两条特殊性照旧：不查工作树、不碰工作树。
        if tui_alive(copy_win) and queue_into(old_id, RECOVER_PROMPT, copy_win, executor):
            entry.pop("closed", None)
            entry["dispatched_at"] = now_iso()
            ledger[args.taskbook] = entry
            save_sessions(ledger)
            print(f"已投进还活着的会话 {old_id}（历史保留，没有开新标签页）")
            return 0
        # 没有 queue 的执行方停在等输入时，关掉再带历史 resume。
        worker = WORKERS[executor]
        if tui_alive(copy_win) and not worker.CAN_QUEUE and worker.parked_at(old_id):
            close_session(copy_win)
        if tui_alive(copy_win):
            print("拒绝：活会话未确认收到消息；先关掉那个标签页再来，不开第二个。")
            return 2
        since = time.time()
        steps = worker.step_count(old_id)
        if not launch_tui(copy_win, RECOVER_PROMPT, args.taskbook, resume_id=old_id,
                          executor=executor):
            print("恢复失败：执行方进程没起来（多为网络问题）；台账不改。")
            return 2
        # 恢复出来的会话是活的，**要重新计进额度**，否则它不占位、下一份就能挤进来。
        entry.pop("closed", None)
        entry["executor"] = executor
        entry["dispatched_at"] = now_iso()
        entry["processes"] = launched_identities(copy_win)
        # 能 queue 的执行方恢复沿用原会话号，不用等。
        for _ in range(0 if worker.CAN_QUEUE else worker.SESSION_WAIT):
            if resumed := newest_session(copy_win, since, executor, old_id, steps):
                entry["session_id"] = resumed
                break
            time.sleep(1)
        ledger[args.taskbook] = entry
        save_sessions(ledger)
        print(f"已恢复（历史保留）。标签页 {args.taskbook}")
        if not entry["processes"]:
            print(f"但没等到 {copy_win} 里的 {executor} 进程：台账没有凭据，"
                  "下一轮派活会把它当成外来会话拒掉；确认标签页起来了就重跑一次 --recover。")
        return 0

    # 轮次到上限就拆。**放在这里而不是别处**：`--close` 与 `--recover` 都已经
    # 在上面返回了——收摊不该被拦，断电恢复更不该，脏树里那份活等着救。
    denied = round_cap_due(book)
    if denied:
        print(denied)
        return 2

    # 合同里也不许贴源码。**交代经 write_note 有这道检查，合同是直接写文件的，
    # 所以补在派活前**——那是合同最后一次能被拦下的时刻，也正是我改它的时刻。
    if fences := source_fences(book.contract_text()):
        print(f"派活拒绝：{book.name} 的合同里有 {len(fences)} 段源码围栏"
              f"（{'、'.join(sorted(set(fences)))}）。\n"
              "**甲方只给目标，不给细节。** 合同贴源码就是把「怎么做」从乙方手上收走。\n"
              "改写成「要成立什么」与「算不算做到了」；字节表、地址、发布数据的形状\n"
              "是事实，用 ```text 或不带标签的围栏照写不误。")
        return 2

    # 额度有限，先看还有没有位置。**和下面那条工作树检查一样，必须紧挨着派活命令。**
    denied = over_quota(args.taskbook, ledger, executor)
    if denied:
        print(denied)
        return 2

    # 派之前先看副本空不空。**这条检查必须紧挨着派活命令**，写在别处会漏。
    #
    # **例外：脏树是这一份自己的会话留下的。** 那不是「别人在上面干活」；
    # 它若还在做本轮，下面会拒派，若已停下，恢复它接着用这棵树。
    # 这条路不碰脏工作树（`merge_main_into` 遇脏树不合），所以脏树不构成风险。
    dirty = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    own_live = (dirty and not entry.get("closed") and entry.get("session_id")
                and same_dir(entry.get("copy"), copy_win) and tui_alive(copy_win))
    # 中途 `--close` 后带新要求重派：脏树仍是这一份的，副本停在它自己的分支上即可认领。
    own_wip = bool(dirty and not own_live and entry.get("closed") and entry.get("session_id")
                   and same_dir(entry.get("copy"), copy_win)
                   and subprocess.run(["git", "-C", str(copy), "rev-parse", "--abbrev-ref", "HEAD"],
                                      capture_output=True, text=True).stdout.strip() == branch)
    if own_wip:
        print(f"{copy.name} 工作树非空，是 {args.taskbook} 中途关掉的会话留下的；"
              f"{'开新会话' if args.fresh else '恢复原会话'}带新一轮提示词，不碰工作树。")
    if dirty and not own_live and not own_wip:
        print(f"{copy.name} 工作树不干净，有人正在上面干活：\n{dirty[:400]}")
        return 2
    if own_live:
        print(f"{copy.name} 工作树非空，是 {args.taskbook} 自己会话留下的；"
              f"还在做本轮就拒派，已停下就带着新一轮提示词恢复它，不碰工作树。")

    # 返工计数只由 --rework 推进；过一轮就清零。满 MAX_ATTEMPTS 强制换新会话：
    # 在一个错里打转时，会话里那段历史正是让它转不出来的东西。
    attempts = int(entry.get("attempts", 0)) + (1 if args.rework else 0)
    old_id = entry.get("session_id")
    burned = attempts >= MAX_ATTEMPTS
    fresh, why_fresh = start_fresh(entry, forced=args.fresh, burned=burned, own_wip=own_wip)

    # 原会话带着历史开新一轮用 `NEW_ROUND_PROMPT`；「继续」只归 `--recover`。
    template = STANDING_PROMPT if fresh else NEW_ROUND_PROMPT
    prompt = template.format(
        # **提示词里指的是它要读的东西**，所以布局说了算：目录布局给目录
        # （它进去看得见 contract / rounds / delivery 各是谁的），单文件给那个 .md。
        taskbook=book.rel(REPO) + ("/" if book.is_dir else ""))

    print(f"任务书 : {args.taskbook}（状态 {status}）")
    print(f"执行方 : {executor}")
    print(f"副本   : {copy}")
    print(f"分支   : {branch}")
    print(f"提示词 : {prompt[:160]}…")
    # **执行方还在做本轮时一律拒派**（各执行方同一条）；拦在写交代之前，免得交代推送了却没人读。
    # 追加的要求只进任务书，等它交回再作为新一轮；严重影响本轮的先 `--close` 停下。
    from shiftboss.inbox import session_parked_at
    if not fresh and tui_alive(copy_win) and not session_parked_at(entry):
        print(f"拒绝：{args.taskbook} 的执行方还在做本轮（会话 {old_id} 未停在等输入）。\n"
              "追加的要求写进任务书，等它交回再派新一轮；严重影响本轮的，先 --close 停下，"
              "再决定重开会话或 --fresh。")
        return 2
    if args.dry_run:
        print("\n--dry-run，没有派出。")
        return 0

    if args.message or new_copy:
        try:
            publish_note(book, args.message, executor, new_copy)
        except (ValueError, subprocess.CalledProcessError) as exc:
            print(str(exc))
            return 2

    # 走到这里一定是新一轮的开头（在做本轮的已在上面拒掉），同步一次（README 4.1 第 5 步）。
    print(merge_main_into(copy, branch))

    # 返工计数只由 --rework 推进；过一轮就清零。满 MAX_ATTEMPTS 强制换新会话：
    # 在一个错里打转时，会话里那段历史正是让它转不出来的东西。
    if fresh and old_id:
        print(f"开新会话（{why_fresh}）；旧会话 {old_id} 的记录留着，要翻回去用它的会话号恢复")

    # 新一轮不往会话里 queue：停在等输入的会话收到 queue 不会开新回合，消息一直排着
    # （实测多条线因此空转数小时）。停着的会话一律关掉，带着新一轮提示词 resume（历史还在）；
    # 烧穿或 `--fresh` 就换新会话。`queue_into` 只留给 `--recover`。
    delivered = False
    if not fresh and tui_alive(copy_win):
        close_session(copy_win)
        print(f"会话 {old_id} 停在等输入，已关掉，改为带着新一轮提示词恢复"
              + (f"（第 {attempts} 次返工）" if args.rework else ""))

    if fresh or not delivered:
        # `resume <id> <prompt>` 带着历史重开；`fresh` 时不带 id，就是全新的干净上下文。
        denied = foreign_session_reason(copy_win, entry)
        if denied:
            print(denied)
            return 2
        if not fresh and tui_alive(copy_win):
            print("拒绝：活会话未确认收到消息；先关掉那个标签页再来，不开第二个。")
            return 2
        if fresh and old_id:
            close_session(copy_win)   # 换新会话之前把旧的关掉，别在同一副本上留两个
        since = time.time()
        steps = None if fresh else WORKERS[executor].step_count(old_id)
        if not launch_tui(copy_win, prompt, args.taskbook, resume_id=None if fresh else old_id,
                          executor=executor):
            print("派活失败：执行方进程没起来（多为网络问题）；台账不记，这一轮的交代已推送，查明后重派。")
            return 2
        new_id = None
        for _ in range(WORKERS[executor].SESSION_WAIT):
            new_id = newest_session(copy_win, since, executor, None if fresh else old_id, steps)
            if new_id:
                break
            time.sleep(1)
        # 恢复不一定新建会话记录；找不到新的就还是原会话。
        new_id = new_id or (None if fresh else old_id)
        if burned:
            print(f"\n返工已满 {MAX_ATTEMPTS} 次，换了新会话（历史丢掉是有意的）")
        entry = {"session_id": new_id, "copy": copy_win, "branch": branch,
                 "processes": launched_identities(copy_win),
                 "attempts": 0 if (args.fresh or burned) else attempts,
                 "updated": today(), "dispatched_at": now_iso()}
    else:
        entry = {"session_id": old_id, "copy": copy_win, "branch": branch,
                 "processes": entry.get("processes", []),
                 "attempts": attempts, "model": entry.get("model"),
                 "updated": today(), "dispatched_at": now_iso()}
    entry["executor"] = executor
    # 新会话此刻多半还没出第一轮，读不到就留空，收件箱现读补上。
    entry["model"] = session_model(entry) or entry.get("model")
    # 每次派出记一条：哪一轮、谁跑、实际模型。上一条的模型在这时补读，那时会话早已跑过。
    previous = ledger.get(args.taskbook) or {}
    history = list(previous.get("history", []))
    if history and previous.get("session_id"):
        history[-1]["model"] = session_model(previous) or history[-1].get("model")
    history.append({"round": book.meta().get("轮次"), "executor": executor,
                    "session_id": entry["session_id"], "model": entry["model"],
                    "dispatched_at": entry["dispatched_at"]})
    entry["history"] = history
    ledger[args.taskbook] = entry
    save_sessions(ledger)
    # **派活是状态真的变了的另一刻，顺手把 STATUS 重生成。**
    # 它整份都是派生的，所以不该靠人记得刷——靠记得就会一直陈着。
    from shiftboss.inbox import origin_git_dir, write_status
    write_status(REPO, origin_git_dir(REPO))

    on = subprocess.run(["git", "-C", str(copy), "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
    print(f"\n已派出。标签页 {args.taskbook}，目标分支 {branch}"
          f"（**由它自己切**；{copy.name} 这一刻在 {on}）")
    print(f"会话 {entry['session_id']}（{executor}，模型 {entry['model'] or '尚未读到'}），"
          f"返工计数 {entry['attempts']}/{MAX_ATTEMPTS}")
    # **派活正是最需要门铃的那一刻**：这一轮的推送要靠它把派活方叫回来。
    # 门铃每响一次就退出、要人重挂，忘了重挂时推送照常进来而派活方一无所知。
    # `verify status` 里已经有同一条提醒，但那要人主动去跑；派出去的回显是必读的，
    # 所以这里也报一次——2026-09-21 连漏两次，第二次是重挂命令被折进复合命令里没起来。
    try:
        from shiftboss.doorbell import listening
        if not listening():
            print("\n⚠ 门铃当前没人听，这一轮的推送不会叫醒你。重挂：")
            print("  shiftboss bell --once --ignore-green "
                  "--heartbeat-minutes 120")
            print("  **单独起一条后台命令**，别折进带 `&` 或重定向的复合命令——"
                  "那样起的进程不受跟踪，退出时叫不醒人。")
    except Exception:
        pass
    # 分支切没切不在这里判：判据是它推上来的那条分支，收件箱从 refs 现算。
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
