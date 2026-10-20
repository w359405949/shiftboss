"""命令行入口：`shiftboss <子命令> [参数]`。

子命令对应包内模块的 `main(argv)`。
"""
from __future__ import annotations

import importlib
import sys

from shiftboss.paths import project_root

COMMANDS = {
    "inbox": "inbox",
    "brief": "brief",       # 派活方每次汇报末尾附的状态摘要
    "ask": "decisions",     # 待用户定夺的事项：登记、列出、记下答复
    "automerge": "automerge",  # 自动合入的线：列出待复核、复核完确认
    "need": "need",         # 待用户补充的数据或证据：登记、列出、记下补充
    "puzzle": "puzzle",     # 派活方的困惑：卡住或反复失败的事，状态摘要列出，不挡收尾
    "taskbook": "taskbook",
    "dispatch": "dispatch",
    "merge": "merge",
    "check": "check",       # 执行方推送前：与合并同一套快速检查
    "bell": "doorbell",
    "recover": "recover",
    "regen": "regen",
    "verify": "verify",
    "patrol": "patrol",     # 常驻巡检：逐页扫全部页面，查到一页红敲一次门铃
    "todo": "todo",         # 派活方待办：带触发条件，状态摘要列出，满足时敲门铃
    "bootstrap": "bootstrap",
    "init": "bootstrap",   # 在项目里装载 shiftboss：与 bootstrap 同一件事
}


def usage() -> str:
    return "用法：shiftboss <子命令> [参数]\n子命令：" + "、".join(COMMANDS)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(usage())
        return 0
    name, rest = args[0], args[1:]
    module = COMMANDS.get(name)
    if module is None:
        print(f"未知子命令：{name}\n{usage()}")
        return 2
    project_root()
    return importlib.import_module(f"shiftboss.{module}").main(rest) or 0


if __name__ == "__main__":
    raise SystemExit(main())
