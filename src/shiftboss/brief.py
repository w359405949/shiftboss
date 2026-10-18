"""派活方每次汇报末尾原样附上的状态摘要：该你处理、待你定、待你补充、在跑、待核对、已合并、排队、派活方待办、门铃、机器、常驻验证、常驻巡检。

Stop 钩子（`shiftboss.hooks.show_brief`）在每次回复结束时现算并显示给用户。
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

from shiftboss import decisions, doorbell, inbox
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
    """负载、常驻验证阶段与巡检状态：页面闸会不会因负载出假红，先看这一行。"""
    import os
    from shiftboss import verify
    load = "／".join(f"{value:.1f}" for value in os.getloadavg())
    stage = ((verify.index_of(git_dir) or {}).get("active") or {}).get("stage") or "空闲"
    return [f"- 负载｜{load}（{os.cpu_count()} 核）｜验证阶段 {stage}"]


def verify_lines(repo) -> list[str]:
    from shiftboss import verify
    report = verify.show_report(inbox.origin_git_dir(repo)).splitlines()
    if not report:
        return ["- 报告｜无"]
    head = report[0].removeprefix("后台验证：")
    ref, _, state = head.partition(" ")
    lines = [f"- 当前｜{ref}｜{state.split('（')[0]}"]
    lines += publication_lines(inbox.origin_git_dir(repo))
    for line in report[1:]:
        text = line.strip()
        if match := re.match(r"最近 (\S+)：(红 \d+ 条)，(较上次验证 [+-]?\d+)", text):
            lines.append(f"- 最近｜{match[1][:12]}｜{match[2]}｜{match[3]}")
        elif match := re.match(r"\[(\w+)\] ([^；]+)；.*负责：(.+)$", text):
            owner = match[3].split(' / ')[0]
            lines.append(f"- 红｜{match[2]}｜{red_owner(inbox.origin_git_dir(repo), owner)}｜{match[1]}")
    return lines


def branch_merged(git_dir, meta: dict) -> bool:
    """执行方分支已推过且已并进 origin 的 main：交回已合并，余下的是续派或收尾。"""
    if not int(meta.get("轮次") or 0):
        return False
    ref = f"refs/heads/{meta.get('分支') or ''}"
    run = lambda *args: subprocess.run(["git", f"--git-dir={git_dir}", *args], capture_output=True).returncode
    return run("rev-parse", "--verify", "--quiet", ref) == 0 and run("merge-base", "--is-ancestor", ref, "refs/heads/main") == 0


def todo_lines(repo, git_dir) -> list[str]:
    from shiftboss import todo
    return todo.summary_lines(repo, git_dir)


def actionable(review, merged, ready, todos, bell, verify, patrol_lines) -> list[str]:
    """派活方现在就能动手的事项：在等条件的不算。Stop 钩子据此拦下收尾，见 `shiftboss.hooks.show_brief`。"""
    items = []
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
    reds = [line for line in verify if line.startswith("- 红｜") and ("无人负责" in line or "修复无效" in line)]
    if reds:
        items.append(f"验证红无主或修复无效 {len(reds)}")
    patrol_reds = [line for line in patrol_lines if line.startswith("- 红 ") and ("｜无人负责" in line or "｜修复无效" in line)]
    if patrol_reds:
        items.append(f"巡检红无主或修复无效 {len(patrol_reds)} 组")
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
    from shiftboss.dispatch import read_quota
    from shiftboss.taskbook import DEPENDENCY_KEYS, Taskbook, unmet_dependencies
    from shiftboss.taskbook import execution_status
    from shiftboss import todo
    running, review, merged, ready, waiting = [], [], [], [], []
    for name, _scheduled, copy, state, pushed, _model in sorted(rows):
        book = Taskbook.find(repo, name)
        waits = tuple(w for key in DEPENDENCY_KEYS if book for w in unmet_dependencies(repo, book, key))
        waits += tuple(f"用户答 {item['id']}" for item in asks if item.get("line") == name)
        waits += tuple(f"用户补充 {item['id']}" for item in needs if item.get("line") == name)
        wait = f"｜等 {'、'.join(waits)}" if waits else ""
        unreviewed = pushed.startswith("可核对") or state.startswith("做完了没推")
        if state.startswith("在干活"):
            running.append(f"{name}｜{Path(copy).name if copy and copy != '—' else '—'}｜{state.removeprefix('在干活').strip('（）')}")
        elif unreviewed and not waits:
            review.append(f"{name}｜{pushed.removeprefix('可核对').strip('（）') if pushed.startswith('可核对') else state}")
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
    bell = bell_lines()
    out += ["【门铃】", *bell, "【机器】", *machine_lines(git_dir)]
    verify = verify_lines(repo)
    out += ["【验证】", *verify]
    from shiftboss import patrol
    patrol_lines = patrol.status_lines(git_dir)
    out += ["【巡检】", *patrol_lines]
    items = actionable(review, merged, ready, todos, bell, verify, patrol_lines)
    print(f"【时间】{datetime.now():%Y-%m-%d %H:%M}")
    # 该你处理放在最前：派活方每次收尾前据此逐项处理，钩子也据此拦下收尾。
    print(f"【该你处理】{'；'.join(items)}" if items else "【该你处理】：无")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
