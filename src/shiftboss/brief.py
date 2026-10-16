"""派活方每次汇报末尾原样附上的状态摘要：待你定、在办任务、门铃与后备闹钟、常驻验证。

Stop 钩子（`shiftboss.hooks.show_brief`）在每次回复结束时现算并显示给用户。
"""
from __future__ import annotations

import time
from datetime import datetime

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
        listen = f"监听中，心跳约 {clock(due)} 到期"
    else:
        listen = "没在监听，需重挂"
    alarms = sorted((proc.create_time() + minutes * 60, minutes)
                    for proc, minutes in doorbell.alarm_processes(path, path))
    alarm = "、".join(f"{minutes} 分（约 {clock(due)}）" for due, minutes in alarms) or "无"
    return [f"【门铃】{listen}；后备闹钟：{alarm}"]


def verify_line(repo) -> str:
    from shiftboss import verify
    lines = verify.show_report(inbox.origin_git_dir(repo)).splitlines()
    head = lines[0].removeprefix("后台验证：") if lines else "无报告"
    latest = next((line.strip().split("；")[0] for line in lines if line.strip().startswith("最近")), "")
    return "【验证】" + head + (f"；{latest}" if latest else "")


def main(argv: list[str] | None = None) -> int:
    repo = project_root()
    git_dir = inbox.origin_git_dir(repo)
    rows, ledger, _active, _live = inbox.status_rows(repo, git_dir)
    print(f"【时间】{datetime.now():%Y-%m-%d %H:%M}")
    # 待你定始终显示：空时写「无」，用户据此区分「确实没有」与「没显示出来」。
    asks = decisions.pending(repo)
    print(f"【待你定】{len(asks)} 件（答复直接回在对话里）" if asks else "【待你定】无")
    for line in decisions.summary_lines(asks):
        print(line)
    from shiftboss.dispatch import read_quota
    from shiftboss.taskbook import DEPENDENCY_KEYS, Taskbook, unmet_dependencies
    running, review, ready, waiting = [], [], [], {}
    for name, _scheduled, _copy, state, pushed, _model in sorted(rows):
        if state.startswith("在干活"):
            running.append(f"{name}（{state.removeprefix('在干活').strip('（）')}）")
        elif pushed.startswith("可核对") or state.startswith("做完了没推"):
            review.append(f"{name}（{pushed.removeprefix('可核对').strip('（）') if pushed.startswith('可核对') else state}）")
        else:
            book = Taskbook.find(repo, name)
            waits = tuple(w for key in DEPENDENCY_KEYS if book for w in unmet_dependencies(repo, book, key))
            if waits:
                waiting.setdefault(waits, []).append(name)
            else:
                ready.append(name)
    print(f"【在跑】{len(running)}/{read_quota()}" + ("" if running else "：无"))
    for item in running:
        print(f"- {item}")
    print(f"【待核对】{len(review)}" + ("" if review else "：无"))
    for item in review:
        print(f"- {item}")
    queued = len(ready) + sum(map(len, waiting.values()))
    print(f"【排队】{queued}" + ("" if queued else "：无"))
    if ready:
        print(f"- 可派（{len(ready)}）：" + "、".join(ready))
    for waits, names in waiting.items():
        print(f"- 等 {'、'.join(waits)}（{len(names)}）：" + "、".join(names))
    for line in bell_lines():
        print(line)
    print(verify_line(repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
