"""派活方待办：带触发条件的事项，状态摘要逐条列出，条件满足时敲一次门铃。

换会话靠它接上在途事项，不靠交接文件；条件由工具现算，不靠派活方记忆。

条件（`--when`）：
    publish:<提交>    常驻验证的最近一次发布已包含该提交
    archived:<任务书>  该任务书已归档
    answered:<编号>    用户已答复该待定事项
    省略              立即可做

用法：
    shiftboss todo add "要做的事" [--when publish:abc123]
    shiftboss todo done <编号>
    shiftboss todo list
"""
from __future__ import annotations

import argparse
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


def save(remote: Path, items: list[dict]) -> None:
    write_state(store(remote), {"items": items})


def add(remote: Path, text: str, when: str | None = None) -> str:
    if when and not re.fullmatch(r"(publish|archived|answered):\S+", when):
        raise ValueError("条件写成 publish:<提交>、archived:<任务书> 或 answered:<编号>")
    items = load(remote)
    number = max((int(item["id"][1:]) for item in items), default=0) + 1
    item = {"id": f"T{number}", "text": text, "when": when, "created": time.time(), "notified": False}
    items.append(item)
    save(remote, items)
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
    if kind == "answered":
        from shiftboss import decisions
        return value not in {item.get("id") for item in decisions.pending(repo)}, f"等用户答 {value}"
    return False, f"条件不认识：{when}"


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
    args = parser.parse_args(argv)
    from shiftboss.verify import origin
    repo = project_root()
    remote = origin(repo)
    if args.command == "add":
        if not args.value:
            parser.error("add 需要要做的事")
        print(f"已登记 {add(remote, args.value, args.when)}")
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
