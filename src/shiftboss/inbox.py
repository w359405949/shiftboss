#!/usr/bin/env python3
"""看有没有执行方交回的活在等着核对。

**为什么不靠会话记忆、也不靠 hook 记事**：bare 仓库的 `refs/heads/codex/*`
本身就是持久事件日志——分支合进 `main` 后自动退出视野，会话换掉、中断、
被替换都不影响。所以这里**每次都从 refs 现算**，不维护「发生过什么」的账本，
只维护「我核到哪了」的游标（`ack`）。

由此得到的两条性质，是这个工具存在的理由：

- **不会有假信号**：判「做完了」看的是分支上任务书里的状态格（合同每条底下那一行），
  不是「有没有 push」。执行方一轮里 push 很多次，只有勾打上了才算交回。
- **不会重复激活**：游标只由 `ack` 前进，`list` 永远只读。所以同一件事
  被看多少次都一样；换个会话重跑 `list`，看到的还是没 ack 的那些。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from shiftboss.state import read_state, write_state

STATUS_LINE = re.compile(r"^\s*-\s*状态[：:]\s*(.+)$")
TICKED = ("☒", "☑", "✅", "[x]", "[X]")
HEADER_CELL = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def _git(git_dir: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        return ""
    return out.stdout


def origin_git_dir(repo: Path) -> Path:
    """origin 的路径从仓库配置里读，不写死——换个项目也能用。"""
    from shiftboss.paths import origin_path, origin_url
    url = origin_url(repo)
    if not url:
        raise SystemExit("没有配置 remote.origin，不知道去哪儿看分支")
    path = origin_path(repo)
    if path is None or not path.exists():
        raise SystemExit(f"origin 不是本机路径，这个工具只认本机 bare 仓库：{url}")
    return path


def taskbook_of(git_dir: Path, branch: str, name: str):
    """从 ref 上取一份任务书 → `(表头字段, 交回正文)`；两种布局都认，没有就 `(None, None)`。

    **布局的事全归 `shiftboss.taskbook`**，这里只提供「怎么从 ref 读一个路径」。
    """
    from shiftboss.taskbook import from_ref
    return from_ref(lambda rel: _git(git_dir, "show", f"{branch}:{rel}"), name)


def meta_of(repo: Path, name: str) -> dict:
    """工作树上那一份的表头字段；找不到给空字典。"""
    from shiftboss.taskbook import Taskbook
    book = Taskbook.find(repo, name)
    return book.meta() if book else {}


def read_status(text: str) -> tuple[int, int, int]:
    """→ (状态格总数, 打了勾的, 标了待议的)。空表示这一条还没做。"""
    total = ticked = pending = 0
    for line in text.splitlines():
        m = STATUS_LINE.match(line)
        if not m:
            continue
        body = m.group(1)
        total += 1
        if any(t in body for t in TICKED):
            ticked += 1
            # 「☒ 待议」「☒ 未做」都不算完成，单独记出来
            for mark in TICKED:
                for label in ("待议", "未做", "做不到"):
                    if re.search(rf"{re.escape(mark)}\s*{label}", body):
                        pending += 1
                        ticked -= 1
                        break
                else:
                    continue
                break
    return total, ticked, pending


def header_value(text: str, key: str) -> str:
    for line in text.splitlines()[:30]:
        m = HEADER_CELL.match(line)
        if m and m.group(1) == key:
            value = m.group(2)
            # 状态常写成 `在办`（常驻，不归档） —— 只取反引号里的那个词
            quoted = re.match(r"\s*`([^`]+)`", value)
            return quoted.group(1) if quoted else value.strip("` ")
    return ""


def survey(repo: Path, git_dir: Path) -> list[dict]:
    """**每次从 refs 现算**，不维护「发生过什么」的账本

    它从 origin 的 `refs/heads/codex/*` 现算谁交回了——**不依赖上一轮会话记得什么**，
    所以会话被换掉、中断、重开都不会漏掉已经做完的活。
    """
    rows = []
    rejected = load_rejected(git_dir)
    # 已并进 main 的一次筛掉：逐分支起 git 进程在 WSL 的 /mnt 上每次要几秒。origin 还没有 main 时全算未并入。
    has_main = subprocess.run(["git", f"--git-dir={git_dir}", "rev-parse", "--verify", "-q",
                               "refs/heads/main"], capture_output=True).returncode == 0
    refs = _git(git_dir, "for-each-ref", *(["--no-merged=main"] if has_main else []),
                "--format=%(refname:short)%09%(committerdate:iso-strict)",
                "refs/heads/codex/*").splitlines()
    for entry in refs:
        if "\t" not in entry:
            continue
        branch, pushed = entry.split("\t", 1)
        name = branch.split("/", 1)[1]
        tip = _git(git_dir, "rev-parse", branch).strip()
        # 退回的轮次永远不会被合并，光靠「并没并进 main」判不出「核过没有」。
        if tip in rejected.get(name, []):
            continue
        meta, text = taskbook_of(git_dir, branch, name)
        if meta is None:
            rows.append(dict(name=name, tip=tip, pushed=pushed, state="没有任务书",
                             total=0, ticked=0, pending=0, copy="", status="", mode="单轮"))
            continue
        total, ticked, pending = read_status(text)
        # 执行状态由执行方写在交回顶部（README 2.4）：完成与停下都等派活方处理。
        # 没写执行状态的存量交回才退回数勾。
        from shiftboss.taskbook import execution_status
        signal = execution_status(text)
        if signal:
            state = "可核对" if signal in ("完成", "停下") else "进行中"
        else:
            state = "可核对" if total and ticked + pending == total else "进行中"
        rows.append(dict(
            name=name, tip=tip, pushed=pushed,
            total=total, ticked=ticked, pending=pending,
            copy=meta.get("你的工作副本", ""),
            status=meta.get("调度状态", ""),
            signal=signal or "未标",
            state=state,
            # 这一行只说分支上的推送；会话死活是另一件事，见 `session_state`。
            #
            # **交付模式必须从 `main` 读，不能读分支上那份。** 表头是派活方写的，
            # 而分支要等执行方下一次合 `origin/main` 才看得到——2026-09-01 加上这一行
            # 的当天就撞了：分析线明明在连做，列表却报「闲置」，正是读了分支上的旧表头。
            mode=(taskbook_of(git_dir, "main", name)[0] or meta).get("交付模式")
                 or "单轮",
        ))
    rows.sort(key=lambda r: (r["state"] != "可核对", r["pushed"]))
    return rows


def ack_rejected(git_dir: Path, name: str, tip: str) -> int:
    """记一笔「这一轮我核过了，结论是退回」。

    **不许记当前顶端**：调用方必须写出自己核的那个 sha。核对与记这一笔之间
    隔着写轮次记录、改任务书、派下一轮，连做的线在这段时间里完全可能又推一轮，
    「记当前顶端」会把那一轮连带标成核过的——2026-09-05 就这么吞过一个 pass。
    """
    branch = f"refs/heads/codex/{name}"
    head = _git(git_dir, "rev-parse", "--verify", "--quiet", branch).strip()
    if not head:
        print(f"origin 上没有 {branch}")
        return 2
    full = _git(git_dir, "rev-parse", "--verify", "--quiet", f"{tip}^{{commit}}").strip()
    if not full:
        print(f"解不出这个提交：{tip}")
        return 2
    on_branch = subprocess.run(
        ["git", f"--git-dir={git_dir}", "merge-base", "--is-ancestor", full, head],
        capture_output=True).returncode == 0
    if not on_branch:
        print(f"{full[:8]} 不在 {branch} 上——**只能退回这条分支上真的推过的东西**。")
        return 2
    if subprocess.run(["git", f"--git-dir={git_dir}", "merge-base",
                       "--is-ancestor", full, "main"],
                      capture_output=True).returncode == 0:
        print(f"{full[:8]} 已经并进 main 了，那是通过、不是退回；用不带 --rejected 的 ack。")
        return 2
    data = load_rejected(git_dir)
    tips = data.setdefault(name, [])
    if full in tips:
        print(f"{name} 的 {full[:8]} 早就记过退回了")
        return 0
    tips.append(full)
    write_state(rejected_path(git_dir), data)
    print(f"记下 {name} 核到 {full[:8]}，结论是退回（不会并进 main，也不再把分支挂成「可核对」）")
    return 0


def ack_path(git_dir: Path) -> Path:
    from shiftboss.config import control_dir
    return control_dir(git_dir) / "inbox-acked.json"


def load_acked(git_dir: Path) -> dict[str, str]:
    return read_state(ack_path(git_dir))


def rejected_path(git_dir: Path) -> Path:
    from shiftboss.config import control_dir
    return control_dir(git_dir) / "inbox-rejected.json"


def load_rejected(git_dir: Path) -> dict[str, list[str]]:
    """核过、**结论是退回**的那些 tip。`{任务书名: [sha, ...]}`

    普通的 ack 只记「已经并进 main 的那一截」——**合并本身就是 ack**。
    可**退回的轮次永远不会被合并**，于是它在收件箱里把分支挂成永久的「可核对」，
    把「这条线还等着我」和「下一轮已经在跑了」混成同一个格子。
    真出过事：一次被吃掉的派活就是靠这一格才看出来的，而这一格当时在说谎。

    所以退回要显式记一笔，且**必须指名道姓写出你核的那个 sha**——
    不接受「记当前顶端」，那正是 2026-09-05 吞掉一个 pass 的写法。
    """
    return read_state(rejected_path(git_dir))


def last_push(git_dir: Path, name: str) -> str:
    """这条线的分支最后一次 push 的时刻；没推过给空串。"""
    return _git(git_dir, "for-each-ref", "--format=%(committerdate:iso-strict)",
                f"refs/heads/codex/{name}").strip()


def session_state(name: str, row: dict | None, entry: dict | None,
                  git_dir: Path) -> str:
    """这条线的会话**现在在干什么**：`在干活`、`做完了没推`、`停着`。

    **会话死活与分支上有没有未合并的推送是两件事，这一格只报前者。**
    后者在「分支」那一列（可核对／进行中／未推分支）。两件曾混在一个词里：
    有未合并推送就一律报「推了等处理」，自走线推完一段接着干的、和早已停下等核对的
    长得一模一样，停下的会话因此被当成在干活、空转数小时没人发现。

    **只说执行状态，不说交付频率，也不说我该干什么。**
    单轮还是连做是任务书自己的属性（表头「交付模式」）。
    把它掺进这一格，执行状态相同的两条线会拿到不同的词，
    同一个词也会指上两种执行状态。
    **要「我该干什么」的，拿这一格和 `keeps_going(交付模式)` 自己合**——
    `recover` 就是这么做的。

    **只用门铃和收件箱判，不看进程、不翻会话 rollout。**
    这条早有结论，写在 `survey()` 里：交付即停是默认，**分支上有还没合并的 push，
    就说明那个副本已经停下等我了**。2026-09-02 我又违反过一次——改用 rollout 的
    `task_complete` 当判据，那仍旧是「看会话那一侧」，而且它连「派出去还没开始推」
    和「跑完停着」都分不开。

    三档（`row` 不参与，只为调用方同一签名保留）：

    - **派出时刻晚于**该分支最后一次 push → 这一轮还没推：会话没停下是 `在干活`，
      已停在等输入是 `做完了没推`。
    - 派出时刻早于最后一次 push → 这一轮推过了：会话没停下仍是 `在干活`
      （连做／自走推完一段还会往下做），已停下是 `停着`。
    """
    sent = (entry or {}).get("dispatched_at")
    if not sent:
        # **台账里没有派出时刻 ≠ 没派过。** `dispatched_at` 是 2026-09-02 才加的，
        # 早于它的那些条目缺这个字段；把缺字段当成「没派过」会报出假的「停着」，
        # 照着它派会往正在干活的副本上再派一轮。**不知道就说不知道**，
        # 下一次派活会把这个字段补上。
        return "停着" if not (entry or {}).get("session_id") else "?（台账缺派出时刻）"
    pushed = last_push(git_dir, name)
    # **这里只用会话时间戳做一次拆分，不用它替代 refs 的判断。**
    # 会话最后一次动静**晚于这一轮派出时刻**、而且停在等输入上，
    # 才说明它确实跑过这一轮然后停住了。早于派出时刻的，是还没接手。
    stopped = _later(session_parked_at(entry), sent)
    # 进程数不出「在干活」，但没停下的会话在副本上没有执行方进程就一定没在干活：
    # 会话中途被杀时 rollout 停不到等输入，只看它会一直报「在干活」。
    # 已停下的不看进程：标签页关没关不改变它在等什么，`recover` 只接回这一档。
    if not stopped and entry and entry.get("copy") and not entry.get("closed"):
        try:
            from shiftboss.dispatch import copy_processes
            if not copy_processes(entry["copy"]):
                return "进程已退出"
        except (RuntimeError, OSError, ImportError):
            pass
    if pushed and sent <= pushed:
        # 这一轮已经推过了。**连做／自走推完一段还会往下做**，而那一段一旦被合掉，
        # `row` 就没了，从 refs 上看它和「交完停住了」一模一样。
        return "停着" if stopped else "在干活"
    # **「在干活」里藏着一种最难发现的坏法：活干完了、提交在本地、就是没推。**
    # 从 refs 看和「还在干」一模一样，会一直挂着直到心跳把我叫去翻会话——
    # 2026-09-07 我就是这么发现一轮活在副本里躺了快一小时的（它停下来问了个问题）。
    return "做完了没推" if stopped else "在干活"


def _later(a: str | None, b: str | None) -> bool:
    """`a` 是否晚于 `b`。**必须解析成时刻再比，不能比字符串**——
    台账写的是本地带偏移（`+08:00`），会话 rollout 写的是 `Z`，
    直接比字符串会把同一时刻判成差 8 小时。
    """
    from datetime import datetime
    try:
        return (datetime.fromisoformat(str(a).replace("Z", "+00:00"))
                > datetime.fromisoformat(str(b).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return False


def session_parked_at(entry: dict | None) -> str | None:
    """台账这条的会话停在等输入上的时刻；还在干活或读不到就是 `None`。

    判据见各执行方插件的 `parked_at`。
    """
    session_id = (entry or {}).get("session_id")
    if not session_id:
        return None
    from shiftboss.dispatch import executor_of
    from shiftboss.workers import WORKERS
    return WORKERS[executor_of(entry.get("executor"))].parked_at(session_id)


STALE_HOURS = 2
"""「在干活」挂多久算该去看一眼。**和门铃心跳同一个数**（`shiftboss.doorbell` 默认 120 分钟）。

两个数必须一起改：心跳把派活方叫起来，叫起来做的事就是过一遍这张表；
门槛比心跳长，那条线就会在两次心跳之间一直显示正常。
2026-09-09 从 120 分钟降到 30——交付变快之后，两小时的门槛等于两小时看不见死在半路的线。"""


def hours_since(stamp: str | None) -> float | None:
    """派出到现在几小时。读不出来返回 None——**不知道就说不知道**，别当成 0。"""
    if not stamp:
        return None
    import datetime
    try:
        sent = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if sent.tzinfo is None:
        sent = sent.astimezone()
    return (datetime.datetime.now(sent.tzinfo) - sent).total_seconds() / 3600


def humanize(hours: float) -> str:
    total = max(0, int(hours * 60))
    return f"{total // 60} 小时 {total % 60} 分" if total >= 60 else f"{total} 分"


def stalled_lines(repo: Path, git_dir: Path,
                  live: dict) -> list[tuple[str, float, str]]:
    """在干活或新一轮还没回音、而且挂了超过 `STALE_HOURS` 的那几条。

    **「死没死」这一半到此为止用收件箱回答，不许去数 `codex.exe` 的进程。**
    进程数不出答案，两个方向都会错：派的是 TUI，做完不退出，**跑完没关的空转
    会话和正在干活的长得一模一样**；反过来会话死了、标签页还开着，进程照样在。
    2026-09-05 我又靠 `Get-Process` 判过一次「死没死」，而台账里的
    `dispatched_at` 早就够回答：分支最后一次 push 早于派出时刻、且派出已经过去
    五个多小时，那就是它没在往前走——那一轮的提示词其实躺在 codex 队列里没人消费。

    判据两条都来自收件箱与台账，和 `session_state` 是同一套：
    执行状态是 `在干活`，**而且最后一次动静（派出与分支最后一次 push 取晚的）
    已经过去 `STALE_HOURS` 小时**。门铃的心跳负责按时提醒，按线的明细在这里。

    **这里不替人判死活**，只把「该去那个标签页看一眼」的那几条挑出来。

    **「死没死」由 `inbox list` 与 `status` 的
       「在干活、超过两小时没动静」那一段报**，判据同样只来自收件箱与台账
       （台账里的派出时刻对分支最后一次 push），
    """
    try:
        from shiftboss.dispatch import load_sessions
        ledger = load_sessions()
    except SystemExit:
        return []
    out = []
    from shiftboss.taskbook import Taskbook
    for book in Taskbook.iter_active(repo):
        entry = ledger.get(book.name)
        if not entry or entry.get("closed"):
            continue
        sent = entry.get("dispatched_at")
        pushed = last_push(git_dir, book.name)
        # **派出晚于最后一次推送就是「新一轮还没回音」**，与分支上有没有未合并的推送无关。
        waiting = bool(sent) and (not pushed or _later(sent, pushed))
        state = session_state(book.name, live.get(book.name), entry, git_dir)
        if state == "进程已退出":
            # 进程没了不用等满门槛：立刻报，`shiftboss recover` 接回。
            pass
        elif not waiting and state != "在干活":
            continue
        # **年龄要从最后一次动静算，不是从派出算。** 拆开执行状态与交付频率之后，
        # 「在干活」既盖「派出后还没推」也盖「推过一段、合掉之后还在往下做」；
        # 后者的派出时刻可以很旧而它几分钟前刚推过，只按派出算会报出假的挂住。
        age = hours_since(pushed if _later(pushed, sent) else sent)
        if state != "进程已退出" and (age is None or age < STALE_HOURS):
            continue
        meta = book.meta()
        out.append((book.name, age or 0.0,
                    (meta.get("你的工作副本") or "").split(chr(92))[-1],
                    started_signs(meta.get("你的工作副本", ""),
                                  meta.get("分支", ""))))
    return out


def started_signs(copy: str, branch: str) -> str:
    """这条线到底**动手了没有**——只看副本里的工作产物，不看会话。

    **「工作树干净」不等于「在慢慢干」。** 2026-09-10 我因为这个把一条根本没起来的
    线放过了一整轮心跳：它一个小时没建目标分支、`out/` 里最新的东西还是我派活时
    自己写的 `STATUS.md`，而我看它工作树干净就当成「在跑浏览器合同，慢是正常的」。
    同一时刻另外三条线的副本都有几十个脏文件——**对照之下差别很明显，
    我只是没去看**。

    两条判据都是执行方的产物，和 `session_state` 同一套口径
    （分支与工作树看得见，会话那一侧看不见）：

    - **目标分支建了没有**：第 0 步就该切过去，没建就是没开始。
    - **工作树动了没有**：改了东西自然有脏文件。

    两条都没有 → 多半真没起来，`--fresh` 重派；有任何一条 → 它在干活，别打断。
    """
    if not copy:
        return "副本未知"
    def git_in(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", copy, *args], capture_output=True,
                                  text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    on_branch = bool(branch) and git_in("rev-parse", "--verify", "--quiet", branch)
    dirty = len([line for line in git_in("status", "--porcelain").splitlines() if line])
    if not on_branch and not dirty:
        return "**目标分支未建、工作树干净——多半没起来**"
    return "、".join(filter(None, [
        "目标分支已建" if on_branch else "目标分支未建",
        f"{dirty} 个脏文件" if dirty else "工作树干净"]))


def origin_has_main(git_dir: Path) -> bool:
    """An absent ref is expected; an unreadable repository must still raise."""
    refs = subprocess.check_output(
        ["git", f"--git-dir={git_dir}", "for-each-ref",
         "--format=%(refname)", "refs/heads/main"],
        text=True, encoding="utf-8").splitlines()
    return "refs/heads/main" in refs


def stale_derived_warning(repo: Path, git_dir: Path) -> list[str]:
    """派生物取了旧侧、后台还没补齐的那笔债。

    合并遇到派生物冲突时整组取旧侧（见 `shiftboss.merge`），
    **这在后台补齐之前是一段真空**：消费端读到的生成物可能缺内容。
    验到那个提交或它的后代之后，这段就消失。
    """
    from shiftboss.state import read_state
    from shiftboss.verify import state_dir
    note = read_state(state_dir(git_dir) / "derived-stale.json")
    commit = (note or {}).get("commit")
    if not commit:
        return []
    index = read_state(state_dir(git_dir) / "index.json")
    verified = index.get("last")
    if verified and subprocess.run(["git", f"--git-dir={git_dir}", "merge-base",
                                    "--is-ancestor", commit, verified],
                                   capture_output=True).returncode == 0:
        return []
    files = (note or {}).get("files") or []
    return ["", f"## ⚠ 派生物取了旧侧，后台还没补齐（合并 {commit[:12]}）", "",
            "这几个生成物现在可能缺内容；消费端读到的就是这一份。"
            "后台验证验到这个提交或它的后代就会补齐。", ""] + \
           [f"- `{name}`" for name in files[:12]] + \
           ([f"- …共 {len(files)} 个"] if len(files) > 12 else []) + [""]


def capacity_text(ledger: dict) -> tuple[str, bool]:
    """各执行方「在跑 X/Y 条，空位 Z 条」，以及是否全部满额。只列一个执行方时不带名字。"""
    from shiftboss.dispatch import capacity
    rows = capacity(ledger)
    parts, full = [], bool(rows)
    for executor, active, quota in rows:
        # **配额 0 ＝ 没给这个执行方分配**，不是「不限」（见 `state.validate_quota`）。
        # 手上还有在跑的线时照实报：那是配额降下来之前派出去的，棘轮不打断它们。
        free = max(0, quota - active)
        full = full and free == 0
        text = (f"在跑 {active}/{quota} 条，空位 {free} 条" if quota
                else f"未分配额度，在跑 {active} 条")
        parts.append(text if len(rows) == 1 else f"{executor} {text}")
    return "；".join(parts), full


def unattended_warning(repo: Path, git_dir: Path, ledger: dict) -> list[str]:
    """Unfinished tasks without an open ledger entry or an unmerged push.

    survey filters rejected tips; this predicate must use all actual branch refs.
    A failed Git read cannot establish that there are no unmerged pushes.
    """
    pending = None
    unattended = []
    from shiftboss.taskbook import Taskbook
    for book in Taskbook.iter_active(repo):
        meta = book.meta()
        status = meta.get("调度状态", "")
        if status not in ("在办", "待派"):
            continue
        entry = ledger.get(book.name)
        if entry is not None and not entry.get("closed"):
            continue
        if pending is None:
            if not origin_has_main(git_dir):
                return ["", "## ⚠ 还没做完、但现在没人在做：算不出", "",
                        "origin 尚无 refs/heads/main，无法判断哪些 push 已合并；"
                        "本段不列名单，其余收件箱照常输出。", ""]
            pending = set(subprocess.check_output(
                ["git", f"--git-dir={git_dir}", "for-each-ref", "--no-merged=refs/heads/main",
                 "--format=%(refname)", "refs/heads/codex/"],
                text=True, encoding="utf-8").splitlines())
        branch = meta.get("分支") or f"codex/{book.name}"
        ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        if ref in pending:
            continue
        copy = meta.get("你的工作副本") or "—"
        unattended.append((book.name, status, copy.replace("\\", "/").rsplit("/", 1)[-1]))
    if not unattended:
        return []
    capacity, full = capacity_text(ledger)
    # **「满载」和「有线空转」可以同时成立**，因为会话关了不等于活做完了。
    # 这两句并排放着时，令人安心的那句会盖过要人动手的那句，于是空转的线一直空转。
    # 所以额度满、却仍有没人做的活时，把这个组合直接说出来，不让读的人自己拼。
    if full:
        capacity = (f"{capacity}——但下面这 {len(unattended)} 条没人在做。"
                    f"额度满不等于都在干活：要么先把某条 `--close` 腾位置，"
                    f"要么确认它们确实该等")
    lines = ["", f"## ⚠ 还没做完、但现在没人在做的 {len(unattended)} 条", "",
             f"**{capacity}。** 请现算基线、补齐这一轮的交代后派下一轮；这里只提醒，不自动派活。", ""]
    lines.extend(f"- `{name}`（`{copy}`）：{status}" for name, status, copy in unattended)
    return lines + [""]


def write_status(repo: Path, git_dir: Path) -> str:
    """现算「现在什么在跑」，返回那段 markdown。

    **每一格都是派生的**（任务书表头 ＋ origin 的分支 ＋ 副本当前 HEAD），
    所以**这份东西不进 git**：文件永远是写入时刻的，进了 git 就是一份带时间戳的
    过期断言——2026-09-01 它还写着某个副本拿着三天前就归档了的任务书，
    照它派活会派到正在干活的副本上。

    权威是 stdout（要看就现跑）；另外落一份 `out/STATUS.md` 只为在编辑器里
    点开方便，`out/` 被 gitignore、且是「唯一可整根删除的目录」，
    没人会把它当权威。

    **`inbox status` 现算并打印「现在什么在跑」。** 它不写进版本库：
    每一格都是派生的，存成文件就是一份带时间戳的过期断言。
    """
    from shiftboss.taskbook import Taskbook
    books = list(Taskbook.iter_active(repo))
    rows = []
    live = {r["name"]: r for r in survey(repo, git_dir)}
    from shiftboss.dispatch import (load_sessions, active_lines, executor_of,
                                   session_model)
    ledger = load_sessions()
    active = active_lines(ledger)
    for book in books:
        if book.name == "STATUS":
            continue
        name = book.name
        meta = book.meta()
        row = live.get(name)
        copy_path = meta.get("你的工作副本", "")
        branch = meta.get("分支", "")
        # **不信状态格，看副本现在实际在哪条分支上。** 常驻任务书派完一轮后状态
        # 常留在「在办」，照它派会派到正在干别的活的副本上。
        on = ""
        if copy_path and Path(copy_path).exists():
            on = subprocess.run(
                ["git", "-C", copy_path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True).stdout.strip()
        # **`--close` 过的不算「在干活」。** 那条线是我主动收摊的，会话没在跑；
        # 台账留着它只为保住会话号。不排掉的话它会顶着「在干活（已 N 小时）」
        # 一直挂在表上，跟真的挂住了的线长得一样。
        entry = ledger.get(name)
        running = session_state(name, row,
                                None if (entry or {}).get("closed") else entry,
                                git_dir)
        # **「在干活」不带年龄就等于没说。** 一分钟前派的和五小时前死掉的
        # 在这一格里长得一样，而分辨这两者正是我上一次去数进程的原因。
        # 年龄从最后一次动静算，理由见 `stalled_lines`。
        if running in ("在干活", "做完了没推"):
            sent = (entry or {}).get("dispatched_at")
            pushed = last_push(git_dir, name)
            age = hours_since(pushed if _later(pushed, sent) else sent)
            if age is not None:
                running += f"（已 {humanize(age)}）"
        rows.append((
            name,
            meta.get("调度状态") or "—",
            copy_path.split(chr(92))[-1] or "—",
            running,
            (f"{row['state']}（{row['signal']}）" if row else "未推分支"),
            "—" if not entry else
            f"{executor_of(entry.get('executor'))} / {session_model(entry) or entry.get('model') or '未读到'}",
        ))
    import datetime
    head = _git(git_dir, "rev-parse", "--short", "main").strip() or "?"
    lines_out = [
        "# 现在什么在跑",
        "",
        f"> **这是 {datetime.datetime.now():%Y-%m-%d %H:%M} 那一刻的快照**"
        f"（`main@{head}`），**不是权威**。",
        "> 要当前的就现跑 `shiftboss inbox status`——",
        "> 每一格都从任务书表头、origin 的 `refs/heads/codex/*` 和副本当前 HEAD 现算。",
        "",
        # 调度状态存在表头（派活方写）；执行状态在交回顶部（执行方写）；会话从进程现算。
        "| 任务书 | 调度状态 | 副本 | 会话 | 提交（执行状态） | 执行方 / 模型 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda r: (not r[4].startswith("可核对"), r[0])):
        lines_out.append("| `{}` | {} | {} | {} | {} | {} |".format(*row))
    # 一份任务书独占一个工作副本。撞车时手维护的表看不见，这里直接报。
    # **不按执行状态过滤。** `rows` 本来就只含在办任务书；而「停着」恰恰是最该报的
    # 那个——它是下一个会被派出去的状态，撞车要到派活那一刻才发作。
    # 单轮线合并时会自动收会话，副本随即算空闲被别的线占走，而它表头还留着原副本，
    # 两份在办任务书就这么指上同一个副本。
    claims: dict[str, list[str]] = {}
    for name, _status, copy, _running, _branch, _model in rows:
        if copy != "—":
            claims.setdefault(copy, []).append(name)
    clashes = {c: n for c, n in claims.items() if len(n) > 1}
    if clashes:
        lines_out += ["", "## ⚠ 同一个副本被多份「在办」任务书认领", ""]
        for copy, names in sorted(clashes.items()):
            lines_out.append(f"- `{copy}`：" + "、".join(f"`{n}`" for n in names))
    stalled = stalled_lines(repo, git_dir, live)
    if stalled:
        lines_out += ["", f"## ⚠ 在干活、超过 {STALE_HOURS:g} 小时没动静", "",
                      "**去那个标签页看一眼；确实死了就 `--fresh` 重派。"
                      "不要去数 `codex.exe`**——理由见 `stalled_lines`。", ""]
        for name, age, copy, signs in stalled:
            lines_out.append(f"- `{name}`（`{copy}`）：已 {humanize(age)}；{signs}")
    lines_out.extend(unattended_warning(repo, git_dir, ledger))
    lines_out += [
        "",
        f"**{capacity_text(ledger)[0]}**"
        + (f"：{'、'.join(active)}" if active else "：（无）")
        + "　—　满了就不再派新的，等手上这些做完 `--close`。"
          "改上限：`shiftboss dispatch --quota N --executor <执行方>`",
        "",
        f"共 {len(rows)} 份在办任务书；归档的见 `archive/`。", ""]
    from shiftboss.dispatch import sandbox_warnings
    lines_out.extend(sandbox_warnings(ledger))
    text = "\n".join(lines_out)
    # 只落进 out/：gitignore 掉的、可整根删除的目录。**不要写回 docs/**。
    target = repo / "out" / "STATUS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return text
    print(f"写好 {target}（{len(rows)} 份）")

def main(argv: list[str] | None = None) -> int:
    """谁交回了、`ack`、装门铃、现算「什么在跑」

    副本闲置状态列：`inbox list` 直接把这一列标出来。
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("list", "ack", "ready", "install-hook", "status"), nargs="?", default="list")
    ap.add_argument("names", nargs="*", help="ack 时给任务书名")
    ap.add_argument("--all", action="store_true", help="连已 ack 的一起列")
    ap.add_argument("--rejected", metavar="TIP",
                    help="ack 时用：这一轮我核过了、结论是退回。"
                         "**必须写出你核的那个 sha**，不接受「记当前顶端」")
    args = ap.parse_args(argv)

    repo = project_root()
    git_dir = origin_git_dir(repo)
    acked = load_acked(git_dir)
    if args.command in ("list", "status"):
        from shiftboss.verify import show_report
        from shiftboss.dispatch import sandbox_warnings, load_sessions
        if args.command == "list":
            for warning in sandbox_warnings(load_sessions()):
                print(warning)
        print(show_report(git_dir) if origin_has_main(git_dir)
              else "后台验证：origin 尚无 refs/heads/main，无法确定验证基线。")
        print()

    if args.command == "status":
        # **权威是这里打出来的这一份**，现算的；out/STATUS.md 只是顺手落的副本。
        print(write_status(repo, git_dir))
        return 0

    if args.command == "install-hook":
        from shiftboss.doorbell import install_hook
        install_hook(git_dir)
        return 0

    if args.command == "ack":
        rows = {r["name"]: r for r in survey(repo, git_dir)}
        if not args.names:
            print("ack 要给任务书名")
            return 2
        if args.rejected:
            if len(args.names) != 1:
                print("--rejected 一次只记一份任务书")
                return 2
            return ack_rejected(git_dir, args.names[0], args.rejected)
        for name in args.names:
            if name not in rows:
                # 合并进 main 的分支会自己退出视野，所以「找不到」通常不是错——
                # **合并本身就是 ack**，只有「核过但还没合」才需要显式记一笔。
                print(f"{name} 不在收件箱里（多半是已经合进 main，自动出队了）")
                continue
            tip = rows[name]["tip"]
            # **只记「已经并进 main 的那一截」，绝不把游标推到没核过的 push 上。**
            # `ack` 拿的是**这一刻**的分支顶端，而核对与 `ack` 之间隔着合并、跑闸、
            # 回填记录——连做的线在这段时间里完全可能又推一轮。2026-09-05 就这么
            # 把一个 pass 吞掉过：核到 e46f03c7，ack 却记成了期间新推的 72c64de1，
            # 那一轮从收件箱里直接消失，是我事后翻 hash 才发现的。
            # 判据是「能不能从本地 main 走到它」——核对靠的就是合并，没并进来就是没核过。
            merged = subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", tip, "main"],
                capture_output=True).returncode == 0
            if not merged:
                base = subprocess.run(
                    ["git", "-C", str(repo), "merge-base", "main", tip],
                    capture_output=True, text=True).stdout.strip()
                ahead = subprocess.run(
                    ["git", "-C", str(repo), "rev-list", "--count", f"main..{tip}"],
                    capture_output=True, text=True).stdout.strip() or "?"
                print(f"{name} 的分支顶端 {tip[:8]} 还没并进 main"
                      f"（比 main 多 {ahead} 笔）——**没并进来就是没核过**，不记它。")
                if base:
                    acked[name] = base
                    print(f"  游标只推到已经并进来的 {base[:8]}；"
                          f"剩下那几笔仍留在收件箱里等你核。")
                continue
            acked[name] = tip
            print(f"记下 {name} 核到 {tip[:8]}")
        write_state(ack_path(git_dir), acked)
        # **ack 是状态真的变了的那一刻，顺手把 STATUS 重生成。**
        # 指望人记得跑一遍，它就会陈掉；而陈掉的索引比没有索引更糟。
        write_status(repo, git_dir)
        return 0

    all_rows = survey(repo, git_dir)
    rows = [r for r in all_rows
            if args.all or acked.get(r["name"]) != r["tip"]]
    live = {r["name"]: r for r in all_rows}

    if args.command == "ready":
        # 给 Monitor 用：一行一个，只报可核对的
        for r in rows:
            if r["state"] == "可核对":
                print(f"{r['name']} {r['tip'][:8]}")
        return 0

    if not rows:
        print("没有新交回的活")
    report_merged_idle(repo, git_dir, rows)
    from shiftboss.dispatch import load_sessions
    print("\n".join(stale_derived_warning(repo, git_dir)
                        + unattended_warning(repo, git_dir, load_sessions())))
    stalled = stalled_lines(repo, git_dir, live)
    if stalled:
        print(f"\n在干活、超过 {STALE_HOURS:g} 小时没动静的 {len(stalled)} 条"
              f"（**去那个标签页看一眼；确实死了就 `--fresh` 重派**）：")
        for name, age, copy, signs in stalled:
            print(f"  {name:<40} 已 {humanize(age):<14} {copy}")
            print(f"  {'':<40} {signs}")
    if not rows:
        return 0
    for state in ("可核对", "进行中", "没有任务书"):
        group = [r for r in rows if r["state"] == state]
        if not group:
            continue
        print(f"\n{state} {len(group)} 条：")
        for r in group:
            mark = "" if r["total"] == 0 else f"{r['ticked']}/{r['total']} 完成"
            if r["pending"]:
                mark += f"  {r['pending']} 待议"
            seen = "" if r["name"] not in acked else "  （之前 ack 过，又推了新的）"
            # **这一列报的是交付频率，不是执行状态**：这些行的执行状态都一样
            # （分支上有未合并的推送），差别只在推完还继不继续。混成一个词出过事，
            # 见 `session_state`。这一列专治「核完忘了派下一轮」。
            from shiftboss.dispatch import keeps_going
            busy = "连做" if keeps_going(r["mode"]) else "单轮"
            print(f"  {r['name']:<40} {mark:<20} {r['pushed'][:16]}"
                  f"  {r['copy'].split(chr(92))[-1]:<18} {busy}{seen}")
    print("\n**「单轮」= 它推完就停了，等你核完再派下一轮。** 核完别忘了派，"
          "不然它就一直空转。")
    return 0


def report_merged_idle(repo: Path, git_dir: Path, rows: list[dict]) -> None:
    """报出「已经合并、现在闲着等派」的那些。

    **这一段专治派活方自己的一个惯犯**：合并 → 推送 → 删分支之后，那条线就从
    收件箱里彻底消失了——**闲置，而且不可见**。2026-09-02 我这么漏过两次，
    第一次是用户先发现的。规则（推过即闲置）只帮我发现，没帮我避免；
    能避免的是让它在同一个命令里一直显形。

    判据要两条一起才成立：**收件箱里没有它未 ack 的 push**，
    **而且它的会话确实停在等输入上**（rollout 末事件是 `task_complete`）。

    **只用前一条会误报**：刚派出去、还没交付的线同样「没有未 ack 的 push」，
    但它在干活。第一版就是这么把正在建写回链的那条报成了闲置。
    「没交付」和「没在做」是两件事，别混。

    **这里不替人判「该不该派」**——低频线、找不到活的线本来就该闲着；
    只负责让它们不至于悄悄消失。
    """
    try:
        from shiftboss.dispatch import load_sessions
        ledger = load_sessions()
    except SystemExit:
        return

    def parked(entry: dict) -> bool:
        """会话停在等输入上才算闲。读不到就当它在干，宁可漏报不要误报。"""
        return session_parked_at(entry) is not None
    from shiftboss.taskbook import Taskbook
    pending = {r["name"] for r in rows}
    idle = []
    for name in sorted(ledger):
        if name in pending or ledger[name].get("closed"):
            continue
        if not parked(ledger[name]):
            continue                   # 还在干活，不是闲置
        book = Taskbook.find(repo, name)
        if book is None or book.archived:   # 已归档的不提
            continue
        meta = book.meta()
        # **白名单按「会不会继续」判，不要按某个词判。** 这里原来只放过「连做」，
        # 于是 2026-09-09 改名叫「自走」的逆向主线每次心跳都被报成
        # 「疑似交付后会话未关闭」——它的会话本来就该一直开着。
        # 同一个错当天在 `merge.close_merged_sessions` 里犯过一次
        # （那边只放过「连做」，一次合并就把自走线关停了）。
        # **只有「单轮」是会停下来等派活方的**，其余一律还在跑。
        # 反复出现的假警报比没有警报更糟：它训练人跳过这一段。
        from shiftboss.dispatch import keeps_going
        if keeps_going(meta.get("交付模式")):
            continue
        idle.append((name, meta.get("调度状态") or "—",
                     (meta.get("你的工作副本") or "").split(chr(92))[-1]))
    if not idle:
        return
    print(f"\n疑似交付后会话未关闭的 {len(idle)} 条（旧台账、绕过 shiftboss.merge 或关闭失败；"
          "核对后用 dispatch --close 收掉）：")
    for name, status, copy in idle:
        print(f"  {name:<40} {status:<16} {copy}")


if __name__ == "__main__":
    # Windows 上默认写 CRLF，CR 会漏进 shell 的字符串比较，让「两份完全
    # 相同的输出」被 comm/diff 判成有差异——假信号就是这么来的。所以固定 LF。
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
