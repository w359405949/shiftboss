"""派活方每次汇报末尾原样附上的状态摘要：待你定、在跑、待核对、已合并、排队、门铃、常驻验证。

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


def verify_lines(repo) -> list[str]:
    from shiftboss import verify
    report = verify.show_report(inbox.origin_git_dir(repo)).splitlines()
    if not report:
        return ["- 报告｜无"]
    head = report[0].removeprefix("后台验证：")
    ref, _, state = head.partition(" ")
    lines = [f"- 当前｜{ref}｜{state.split('（')[0]}"]
    for line in report[1:]:
        text = line.strip()
        if match := re.match(r"最近 (\S+)：(红 \d+ 条)，(较上次验证 [+-]?\d+)", text):
            lines.append(f"- 最近｜{match[1][:12]}｜{match[2]}｜{match[3]}")
        elif match := re.match(r"\[(\w+)\] ([^；]+)；.*负责：(.+)$", text):
            lines.append(f"- 红｜{match[2]}｜{match[3].split(' / ')[0]}｜{match[1]}")
    return lines


def branch_merged(git_dir, meta: dict) -> bool:
    """执行方分支已推过且已并进 origin 的 main：交回已合并，余下的是续派或收尾。"""
    if not int(meta.get("轮次") or 0):
        return False
    ref = f"refs/heads/{meta.get('分支') or ''}"
    run = lambda *args: subprocess.run(["git", f"--git-dir={git_dir}", *args], capture_output=True).returncode
    return run("rev-parse", "--verify", "--quiet", ref) == 0 and run("merge-base", "--is-ancestor", ref, "refs/heads/main") == 0


def main(argv: list[str] | None = None) -> int:
    repo = project_root()
    git_dir = inbox.origin_git_dir(repo)
    rows, ledger, _active, _live = inbox.status_rows(repo, git_dir)
    print(f"【时间】{datetime.now():%Y-%m-%d %H:%M}")
    # 待你定始终显示：空时写「无」，用户据此区分「确实没有」与「没显示出来」。
    asks = decisions.pending(repo)
    print(f"【待你定】{len(asks)}" if asks else "【待你定】：无")
    for line in decisions.summary_lines(asks):
        print(line)
    from shiftboss.dispatch import read_quota
    from shiftboss.taskbook import DEPENDENCY_KEYS, Taskbook, unmet_dependencies
    from shiftboss.taskbook import execution_status
    running, review, merged, ready, waiting = [], [], [], [], []
    for name, _scheduled, copy, state, pushed, _model in sorted(rows):
        book = Taskbook.find(repo, name)
        waits = tuple(w for key in DEPENDENCY_KEYS if book for w in unmet_dependencies(repo, book, key))
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
        print(f"【{title}】{len(items)}{suffix}" + ("" if items else "：无"))
        for item in items:
            print(f"- {item}")
    print("【门铃】")
    for line in bell_lines():
        print(line)
    print("【验证】")
    for line in verify_lines(repo):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
