"""派活方待办：带触发条件的事项，状态摘要逐条列出，条件满足时敲一次门铃。

换会话靠它接上在途事项，不靠交接文件；条件由工具现算，不靠派活方记忆。

条件（`--when`）：
    publish:<提交>    常驻验证的最近一次发布已包含该提交
    archived:<任务书>  该任务书已归档
    answered:<编号>    用户已答复该待定事项（D）或已补充（E）
    after:<时长>       登记后过了这么久（如 30m、2h），用于隔段时间重试
    load:<上限>        本机 5 分钟平均负载低于上限（如 load:12），用于等低负载实测
    省略              立即可做

用法：
    shiftboss todo add "要做的事" [--when publish:abc123] [--line 任务书名]
    shiftboss todo done <编号>
    shiftboss todo list
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from shiftboss import config
from shiftboss.paths import project_root
from shiftboss.state import read_state, write_state


def store(remote: Path) -> Path:
    return config.control_dir(remote) / "todo.json"


def load(remote: Path) -> list[dict]:
    return (read_state(store(remote)) or {}).get("items", [])


def save(remote: Path, items: list[dict], last: int | None = None) -> None:
    # 编号只增不复用：了结的编号再出现会让简报与汇报里的指代混淆。
    previous = (read_state(store(remote)) or {}).get("last", 0)
    numbers = [int(item["id"][1:]) for item in items]
    write_state(store(remote), {"items": items, "last": max([previous, last or 0, *numbers])})


def add(remote: Path, text: str, when: str | None = None, line: str | None = None) -> str:
    if when and (match := re.fullmatch(r"after:(\d+)([mh])", when)):
        when = f"at:{int(time.time()) + int(match[1]) * (60 if match[2] == 'm' else 3600)}"
    elif when and not re.fullmatch(r"(publish|archived|answered):\S+|load:\d+(\.\d+)?", when):
        raise ValueError("条件写成 publish:<提交>、archived:<任务书>、answered:<编号>、after:<分钟m|小时h> 或 load:<上限>")
    items = load(remote)
    number = max([(read_state(store(remote)) or {}).get("last", 0),
                  *(int(item["id"][1:]) for item in items)]) + 1
    item = {"id": f"T{number}", "text": text, "when": when, "created": time.time(), "notified": False}
    if line:
        item["line"] = line
    items.append(item)
    save(remote, items, number)
    return item["id"]


def done(remote: Path, ident: str) -> None:
    items = load(remote)
    kept = [item for item in items if item["id"] != ident]
    if len(kept) == len(items):
        raise ValueError(f"没有待办 {ident}")
    save(remote, kept)


def _git(remote: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", f"--git-dir={remote}", *args], capture_output=True, text=True)


def condition(remote: Path, repo: Path, when: str | None) -> tuple[bool, str]:
    """→（是否满足，等待说明）。"""
    if not when:
        return True, ""
    kind, _, value = when.partition(":")
    if kind == "publish":
        from shiftboss import verify
        published = (verify.index_of(remote) or {}).get("last_consistent")
        ok = bool(published) and _git(remote, "merge-base", "--is-ancestor", value, published).returncode == 0
        return ok, f"等发布越过 {value[:10]}"
    if kind == "archived":
        from shiftboss.taskbook import Taskbook
        book = Taskbook.find(repo, value)
        return bool(book and book.archived), f"等 {value} 归档"
    if kind == "at":
        return time.time() >= int(value), f"等到 {time.strftime('%H:%M', time.localtime(int(value)))}"
    if kind == "load":
        current = os.getloadavg()[1]
        return current < float(value), f"等负载低于 {value}（现 {current:.1f}）"
    if kind == "answered":
        from shiftboss import decisions
        open_ids = {item.get("id") for item in decisions.load(repo) if item.get("answer") is None}
        return value not in open_ids, f"等用户{'补充' if value.startswith('E') else '答'} {value}"
    return False, f"条件不认识：{when}"


def line_waits(repo: Path, remote: Path, name: str) -> list[str]:
    """关联到该线、条件未满足的待办：状态摘要据此把线列入排队，不算待核对（如读取方等生成器段发布）。"""
    waits = []
    for item in load(remote):
        if item.get("line") == name:
            ok, wait = condition(remote, repo, item.get("when"))
            if not ok:
                waits.append(f"{item['id']} {wait}")
    return waits


def summary_lines(repo: Path, remote: Path) -> list[str]:
    lines = []
    for item in load(remote):
        ok, wait = condition(remote, repo, item.get("when"))
        lines.append(f"- {item['id']}｜{'可做' if ok else wait}｜{item['text']}")
    return lines


def ripe(repo: Path, remote: Path) -> list[dict]:
    """条件刚满足、尚未敲过门铃的待办；调用方敲铃后它们记为已通知。"""
    items, fresh = load(remote), []
    for item in items:
        if not item.get("notified") and item.get("when") and condition(remote, repo, item["when"])[0]:
            item["notified"] = True
            fresh.append(item)
    if fresh:
        save(remote, items)
    return fresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["add", "done", "list"])
    parser.add_argument("value", nargs="?", help="add：要做的事；done：编号")
    parser.add_argument("--when", help="触发条件，见上")
    parser.add_argument("--line", help="关联的任务书名：条件未满足时状态摘要把该线列入排队")
    args = parser.parse_args(argv)
    from shiftboss.verify import origin
    repo = project_root()
    remote = origin(repo)
    if args.command == "add":
        if not args.value:
            parser.error("add 需要要做的事")
        print(f"已登记 {add(remote, args.value, args.when, args.line)}")
    elif args.command == "done":
        if not args.value:
            parser.error("done 需要编号")
        done(remote, args.value)
        print(f"已完成 {args.value}")
    else:
        print("\n".join(summary_lines(repo, remote)) or "没有待办")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
