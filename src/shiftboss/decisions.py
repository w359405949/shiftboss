"""待用户定夺的事项：派活方登记后照常推进别的活，用户随时在状态摘要里看到并答复。

用户不一直盯着会话；弹出选择框会让派活方停下等待，期间别的活全部积压。所以需要用户决定的事
一律登记在这里，由 Stop 钩子的状态摘要（`shiftboss brief`）每次列出，用户答复后派活方 `--resolve` 记下答复。

用法：
    shiftboss ask "问题" --option "甲：说明" --option "乙：说明" [--recommend 1] [--line 任务书名]
    shiftboss ask --list
    shiftboss ask --resolve <编号> "用户的答复"
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from shiftboss.config import control_dir
from shiftboss.paths import project_root


def store_path(repo: Path | None = None) -> Path:
    from shiftboss import inbox
    repo = repo or project_root()
    return control_dir(inbox.origin_git_dir(repo)) / "decisions.json"


def load(repo: Path | None = None) -> list[dict]:
    path = store_path(repo)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save(items: list[dict], repo: Path | None = None) -> None:
    path = store_path(repo)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pending(repo: Path | None = None) -> list[dict]:
    return [item for item in load(repo) if item.get("answer") is None]


def summary_lines(items: list[dict]) -> list[str]:
    """状态摘要里每条一行：编号｜登记时间｜问题｜选项（推荐项标出）｜关联任务书。"""
    lines = []
    for item in items:
        cells = [item["id"], item["asked"][5:16].replace("T", " "), item["question"]]
        if item.get("options"):
            cells.append("；".join(f"{index}) {text}" + ("（推荐）" if index == item.get("recommend") else "")
                                  for index, text in enumerate(item["options"], 1)))
        if item.get("line"):
            cells.append(f"关联 {item['line']}")
        lines.append("- " + "｜".join(cells))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shiftboss ask", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="?", help="要用户定夺的问题；与 --resolve 连用时为用户的答复")
    parser.add_argument("--option", action="append", default=[], help="一个选项，可重复")
    parser.add_argument("--recommend", type=int, help="推荐的选项序号（从 1 起）")
    parser.add_argument("--line", help="关联的任务书名")
    parser.add_argument("--list", action="store_true", help="列出未答复的事项")
    parser.add_argument("--resolve", metavar="编号", help="记下用户对该事项的答复")
    args = parser.parse_args(argv)
    items = load()
    if args.resolve:
        item = next((item for item in items if item["id"] == args.resolve), None)
        if item is None:
            raise SystemExit(f"没有待定事项 {args.resolve}")
        if not args.question:
            raise SystemExit("--resolve 须给出用户的答复原文")
        item["answer"] = args.question
        item["answered"] = datetime.now().isoformat(timespec="seconds")
        save(items)
        print(f"已记下 {item['id']} 的答复")
        return 0
    if args.list or not args.question:
        lines = summary_lines(pending())
        print("\n".join(lines) if lines else "没有待你定的事项")
        return 0
    if args.recommend is not None and not 1 <= args.recommend <= len(args.option):
        raise SystemExit("--recommend 须是已给选项的序号")
    number = max((int(item["id"].lstrip("D")) for item in items), default=0) + 1
    item = {"id": f"D{number}", "asked": datetime.now().isoformat(timespec="seconds"),
            "question": args.question, "options": args.option, "recommend": args.recommend,
            "line": args.line, "answer": None}
    items.append(item)
    save(items)
    print(f"已登记 {item['id']}：{args.question}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
