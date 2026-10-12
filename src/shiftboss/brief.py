"""派活方每次汇报末尾原样附上的状态摘要：在办任务、门铃与后备闹钟、常驻验证。

Stop 钩子（`shiftboss.hooks.show_brief`）在每次回复结束时现算并显示给用户。
"""
from __future__ import annotations

import time
from datetime import datetime

import psutil

from shiftboss import doorbell, inbox
from shiftboss.paths import project_root

MARKER = "【在办】"


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
    rows, _ledger, _active, _live = inbox.status_rows(repo, git_dir)
    print(f"【时间】{datetime.now():%Y-%m-%d %H:%M}")
    print(MARKER)
    from shiftboss.taskbook import DEPENDENCY_KEYS, Taskbook, unmet_dependencies
    for name, scheduled, copy, running, pushed, _model in sorted(rows):
        book = Taskbook.find(repo, name)
        waits = [f"{key}：{'、'.join(pending)}" for key in DEPENDENCY_KEYS
                 if book and (pending := unmet_dependencies(repo, book, key))]
        print(f"- {name}｜{running}｜{pushed}｜{scheduled}" + (f"｜等 {'；'.join(waits)}" if waits else ""))
    if not rows:
        print("- （无）")
    for line in bell_lines():
        print(line)
    print(verify_line(repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
