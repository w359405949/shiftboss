"""派活方每次汇报末尾原样附上的状态摘要：该你处理、待你定、待你补充、我的困惑、在跑、待核对、已合并、排队、派活方待办、门铃、机器、常驻验证、常驻巡检。

Stop 钩子（`shiftboss.hooks.show_brief`）在每次回复结束时现算并显示给用户。
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

from shiftboss import config, decisions, doorbell, inbox
from shiftboss.paths import project_root



def clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def bell_lines() -> list[str]:
    path = doorbell.default_path()
    listeners = doorbell.rival_listeners(path, path)
    if listeners:
        started = min(proc.create_time() for proc in listeners)
        due = max(doorbell.last_event(path), started) + doorbell.HEARTBEAT_MINUTES * 60
        lines = [f"- 监听｜在｜心跳 {clock(due)}"]
    else:
        lines = ["- 监听｜不在｜需重挂"]
    return lines


def publication_lines(git_dir) -> list[str]:
    """最近一次发布（派生物与源一致）的提交，及 main 领先它几个第一父链提交：发布落后时先排除这一因。

    最近一轮生成阶段失败时标出「生成失败，发布停滞」：发布落后不一定是还没轮到，
    生成每轮都失败时等多久都发布不出来，须按生成失败开线。
    """
    from shiftboss import verify
    from shiftboss.state import read_state
    index = verify.index_of(git_dir) or {}
    sha = index.get("last_consistent")
    if not sha:
        return ["- 发布｜无记录"]
    run = lambda *args: subprocess.run(["git", f"--git-dir={git_dir}", *args],
                                       capture_output=True, text=True).stdout.strip()
    when = run("log", "-1", "--format=%cd", "--date=format:%m-%d %H:%M", sha)
    behind = run("rev-list", "--count", "--first-parent", f"{sha}..refs/heads/main")
    report = read_state(verify.state_dir(git_dir) / "runs" / f"{index.get('last')}.json") if index.get("last") else None
    stalled = any(item.get("id") == "stage:generate" for item in (report or {}).get("failures", []))
    return [f"- 发布｜{sha[:12]}｜{when}｜main 领先 {behind or '?'} 个提交" + ("｜生成失败，发布停滞" if stalled else "")]


def red_owner(git_dir, owner: str) -> str:
    """验证红页的负责线及其状态：负责线已归档时，按其合并提交是否已在最近验过的提交里分「待复扫」「修复无效」。"""
    if owner.startswith("派活方"):
        return "无人负责"
    from shiftboss import patrol, verify
    info = patrol.owner(git_dir, owner)
    if info is None:
        return f"{owner}（main 上找不到）"
    if not info["archived"]:
        return f"{owner}（在修）"
    verified = (verify.index_of(git_dir) or {}).get("last")
    if not info["merge"] or not verified:
        return f"{owner}（已归档）"
    scanned = subprocess.run(["git", f"--git-dir={git_dir}", "merge-base", "--is-ancestor", info["merge"], verified],
                             capture_output=True).returncode == 0
    return f"{owner}（已归档，{'修复无效，须另开线' if scanned else '待复扫'}）"


def machine_lines(git_dir) -> list[str]:
    """本机负载：性能数据可不可比，先看这一行。"""
    import os
    load = "／".join(f"{value:.1f}" for value in os.getloadavg())
    return [f"- 负载｜{load}（{os.cpu_count()} 核）"]


def ratchet_lines(repo) -> list[str]:
    """棘轮基线余量（`[brief] ratchets`）：迁移进度看这一行，不逐次手算。"""
    import json
    lines = []
    for rel in config.ratchet_dirs(repo):
        counts = {}
        for path in sorted((repo / rel).glob("*.json")):
            entries = json.loads(path.read_text(encoding="utf-8"))
            counts[path.stem] = sum(e.get("count", 1) for e in entries) if isinstance(entries, list) else 0
        left = sorted(((n, c) for n, c in counts.items() if c), key=lambda item: -item[1])
        detail = "、".join(f"{n} {c}" for n, c in left) or "清零"
        lines.append(f"- {Path(rel).name}｜{sum(counts.values())}｜{detail}")
    return lines


def mistake_lines(git_dir) -> list[str]:
    """错题本（控制目录 `mistakes.md`）：条目数、再犯次数与最近一条的标题，看差错是否在减少。"""
    path = config.control_dir(git_dir) / "mistakes.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    titles = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
    again = sum(1 for line in text.splitlines() if line.lstrip("- ").startswith("再犯"))
    return [f"【错题】{len(titles)} 条，再犯 {again} 次" + (f"｜最近：{titles[-1]}" if titles else "")]


def verify_lines(repo) -> list[str]:
    from shiftboss import verify
    report = verify.show_report(inbox.origin_git_dir(repo)).splitlines()
    if not report:
        return ["- 报告｜无"]
    head = report[0].removeprefix("后台验证：")
    ref, _, state = head.partition(" ")
    git_dir = inbox.origin_git_dir(repo)
    stage = ((verify.index_of(git_dir) or {}).get("active") or {}).get("stage") or "空闲"
    lock = verify.state_dir(git_dir) / "worker.lock"
    pid = lock.read_text(encoding="utf-8").strip() if lock.exists() else ""
    alive = "在跑" if pid.isdigit() and psutil.pid_exists(int(pid)) else "没在跑"
    lines = [f"- 工具闸｜{alive}｜阶段 {stage}｜{ref} {state.split('（')[0]}"]
    lines += publication_lines(inbox.origin_git_dir(repo))
    for line in report[1:]:
        text = line.strip()
        if match := re.match(r"最近 (\S+)：(红 \d+ 条)，(较上次验证 [+-]?\d+)", text):
            lines.append(f"- 工具闸最近｜{match[1][:12]}｜{match[2]}｜{match[3]}")
        elif match := re.match(r"\[(\w+)\] ([^；]+)；.*负责：(.+)$", text):
            owner = match[3].split(' / ')[0]
            lines.append(f"- 工具闸红｜{match[2]}｜{red_owner(inbox.origin_git_dir(repo), owner)}｜{match[1]}")
    return lines


def branch_merged(git_dir, meta: dict) -> bool:
    """执行方分支已推过且已并进 origin 的 main：交回已合并，余下的是续派或收尾。"""
    if not int(meta.get("轮次") or 0):
        return False
    ref = f"refs/heads/{meta.get('分支') or ''}"
    run = lambda *args: subprocess.run(["git", f"--git-dir={git_dir}", *args], capture_output=True).returncode
    return run("rev-parse", "--verify", "--quiet", ref) == 0 and run("merge-base", "--is-ancestor", ref, "refs/heads/main") == 0


def idle_copy(copy: str) -> bool:
    """副本工作树干净且没有领先 origin/main 的提交。"""
    if not copy or copy == "—" or not Path(copy).is_dir():
        return False
    run = lambda *args: subprocess.run(["git", "-C", copy, *args], capture_output=True, text=True).stdout.strip()
    return not run("status", "--porcelain") and run("rev-list", "--count", "origin/main..HEAD") in ("0", "")


def todo_lines(repo, git_dir) -> list[str]:
    from shiftboss import todo
    return todo.summary_lines(repo, git_dir)


def actionable(review, merged, ready, todos, bell, verify, patrol_lines, automerged=()) -> list[str]:
    """派活方现在就能动手的事项：在等条件的不算。Stop 钩子据此拦下收尾，见 `shiftboss.hooks.show_brief`。"""
    items = []
    if automerged:
        items.append("复核自动合入 " + "、".join(automerged))
    if review:
        items.append(f"待核对 {len(review)}")
    follow = [line.split("｜")[0] for line in merged if "｜等 " not in line]
    if follow:
        items.append("待续派/收尾 " + "、".join(follow))
    if ready:
        items.append(f"可派 {len(ready)}")
    ripe = [line.split("｜")[0].removeprefix("- ") for line in todos if "｜可做｜" in line]
    if ripe:
        items.append("待办 " + "、".join(ripe))
    # 待办正文写明了票据号的红已由派活方带条件跟进（如「下一轮仍红再开线」），不再计入。
    followed = " ".join(todos)
    reds = [line for line in verify if line.startswith("- 工具闸红｜") and ("无人负责" in line or "修复无效" in line)
            and line.rsplit("｜", 1)[-1] not in followed]
    if reds:
        items.append(f"工具闸红无主或修复无效 {len(reds)}")
    patrol_reds = [line for line in patrol_lines if line.startswith("- 页面红 ") and ("｜无人负责" in line or "｜修复无效" in line)]
    if patrol_reds:
        items.append(f"页面红无主或修复无效 {len(patrol_reds)} 组")
    if any("不在" in line for line in bell):
        items.append("门铃不在")
    return items


def main(argv: list[str] | None = None) -> int:
    repo = project_root()
    git_dir = inbox.origin_git_dir(repo)
    rows, ledger, _active, _live = inbox.status_rows(repo, git_dir)
    out = []
    # 待你定始终显示：空时写「无」，用户据此区分「确实没有」与「没显示出来」。
    asks = decisions.pending(repo)
    out.append(f"【待你定】{len(asks)}" if asks else "【待你定】：无")
    out += decisions.summary_lines(asks)
    needs = decisions.pending(repo, kind="need")
    out.append(f"【待你补充】{len(needs)}" if needs else "【待你补充】：无")
    out += decisions.summary_lines(needs)
    # 困惑不等答复、不计入「该你处理」：只让用户看见派活方卡在哪。
    puzzles = decisions.pending(repo, kind="puzzle")
    out.append(f"【我的困惑】{len(puzzles)}" if puzzles else "【我的困惑】：无")
    out += decisions.summary_lines(puzzles)
    out += mistake_lines(git_dir)
    from shiftboss.dispatch import read_quota
    from shiftboss.taskbook import DEPENDENCY_KEYS, Taskbook, unmet_dependencies
    from shiftboss.taskbook import execution_status
    from shiftboss import todo
    running, review, merged, ready, waiting = [], [], [], [], []
    from shiftboss import automerge
    try:
        awaiting = automerge.awaiting(repo, git_dir)
    except SystemExit:
        awaiting = {}
    for name, _scheduled, copy, state, pushed, _model in sorted(rows):
        book = Taskbook.find(repo, name)
        dependency_waits = tuple(w for key in DEPENDENCY_KEYS if book for w in unmet_dependencies(repo, book, key))
        dependency_waits += tuple(todo.line_waits(repo, git_dir, name))
        # 等用户答复只挡派活，不挡核对已交回的分支。
        waits = dependency_waits + tuple(f"用户答 {item['id']}" for item in asks if item.get("line") == name)
        waits += tuple(f"用户补充 {item['id']}" for item in needs if item.get("line") == name)
        waits += tuple(f"困惑 {item['id']}" for item in puzzles if name in (item.get("line") or "").split(","))
        wait = f"｜等 {'、'.join(waits)}" if waits else ""
        unreviewed = pushed.startswith("可核对") or state.startswith("做完了没推")
        if state.startswith("在干活"):
            running.append(f"{name}｜{Path(copy).name if copy and copy != '—' else '—'}｜{state.removeprefix('在干活').strip('（）')}")
        elif unreviewed and name in awaiting:
            waiting.append(f"{name}｜等巡检自动合入")
        elif unreviewed and not dependency_waits:
            label = pushed.removeprefix('可核对').strip('（）') if pushed.startswith('可核对') else state
            # 会话停下却一行未改、未推：执行方把进度说明当成了收尾，续同一会话即可，不是交回。
            if state.startswith("做完了没推") and pushed.startswith("未推分支") and idle_copy(copy):
                label += f"｜空交回：shiftboss dispatch {name} --recover 续"
            review.append(f"{name}｜{label}")
        elif book and not unreviewed and branch_merged(git_dir, book.meta()):
            status = execution_status(book.delivery_text()) or "—"
            follow = "待收尾" if status == "完成" else "待续派"
            merged.append(f"{name}｜{status}｜{follow}{wait}")
        elif waits:
            waiting.append(f"{name}{wait}" + ("｜分支待核" if unreviewed else ""))
        else:
            ready.append(f"{name}｜可派" + ("｜分支有未合进展" if pushed.startswith("进行中") else ""))
    sections = [("在跑", running, f"/{read_quota()} 会话"),
                ("待核对", review, ""), ("已合并", merged, ""), ("排队", ready + waiting, "")]
    for title, items, suffix in sections:
        out.append(f"【{title}】{len(items)}{suffix}" + ("" if items else "：无"))
        out += [f"- {item}" for item in items]
    todos = todo_lines(repo, git_dir)
    out.append(f"【派活方待办】{len(todos)}" + ("" if todos else "：无"))
    out += todos
    # 常驻验证的三条并行支线（D53）：工具闸、页面、定时。
    from shiftboss import patrol
    verify = verify_lines(repo)
    patrol_lines = patrol.page_lines(git_dir)
    out += ["【常驻验证】", *verify, *patrol_lines, *patrol.periodic_lines(git_dir)]
    if ratchets := ratchet_lines(repo):
        out += ["【棘轮余量】", *ratchets]
    if slowest := patrol.slowest_lines(git_dir):
        out += ["【性能】", *slowest]
    bell = bell_lines()
    out += ["【门铃】", *bell, "【机器】", *machine_lines(git_dir)]
    automerged = automerge.unreviewed(git_dir)
    items = actionable(review, merged, ready, todos, bell, verify, patrol_lines, automerged)
    print(f"【时间】{datetime.now():%Y-%m-%d %H:%M}")
    # 该你处理放在最前：派活方每次收尾前据此逐项处理，钩子也据此拦下收尾。
    print(f"【该你处理】{'；'.join(items)}" if items else "【该你处理】：无")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
