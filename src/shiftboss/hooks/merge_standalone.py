"""`shiftboss merge` 只能单独一条命令 —— 这个钩子拦住把它和别的命令串在一起的写法。

**为什么**：合并可能冲突撤回，也可能停在 MERGE_HEAD 留现场。串在同一条命令里的
`git add`／`commit`／`push`／`dispatch`／`--done` 不看合并结果照样执行，曾把带冲突标记的
文件、未过快速检查的合并推上 main，并据此派出依赖它的线；过滤输出（`grep`）还会
把失败行滤掉。备忘拦不住，已发生过多次。

允许的形态：可选的 `cd <目录> &&` 前缀加一条 `shiftboss merge …`，可接 `2>&1` 与
`| tail -N`（只截尾，不滤行）。
"""

from __future__ import annotations

import json
import re
import sys

MERGE = re.compile(r"\bshiftboss(?:\s+|\.(?:cli\s+)?)merge\b")
STANDALONE = re.compile(
    r"^\s*(?:cd\s+[^;&|<>]+?\s*&&\s*)?shiftboss\s+merge(?:\s+[^;&|<>`$()]*?)?"
    r"(?:\s+2>&1)?(?:\s*\|\s*tail\s+(?:-n\s*)?-?\d+)?\s*$"
)

ADVICE = (
    "合并单独一条命令，看到「合并完成」或「已归档」再做下一步：\n"
    "    command: cd <项目根> && shiftboss merge <分支或提交> 2>&1 | tail -15\n"
    "推送、写轮次、派活、--done 各自另起一条。"
)


def offending(tool_input: dict) -> str | None:
    command = str(tool_input.get("command", ""))
    if not MERGE.search(command) or "--help" in command:
        return None
    if not STANDALONE.match(command):
        return "合并和别的命令串在了一起"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    hit = offending(payload.get("tool_input") or {})
    if hit is None:
        return 0
    print(f"{hit}：这条被工作流拦下了。\n{ADVICE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
