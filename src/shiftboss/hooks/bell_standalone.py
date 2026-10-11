"""门铃只能单独以后台命令挂起，且后台时限取上限 —— 这个钩子拦住其他挂法。

**为什么**：门铃要等执行方交回，常超过半小时。
- 折进带 `&`、`;`、`|` 或重定向的复合命令，起的进程不受跟踪，退出时叫不醒派活方；
- 不给 `timeout`，后台命令默认 30 分钟被杀。
两种挂法都让派活方漏掉交回，且各已发生过不止一次，备忘拦不住。

允许的形态：可选的 `cd <目录> &&` 前缀加一条 `shiftboss bell …`，`run_in_background` 为真，
`timeout` 为 21600000。
"""

from __future__ import annotations

import json
import re
import sys

BELL = re.compile(r"\bshiftboss(?:\s+|\.)bell\b")
STANDALONE = re.compile(r"^\s*(?:cd\s+[^;&|<>]+?\s*&&\s*)?shiftboss\s+bell(?:\s+[^;&|<>`$()]*)?\s*$")
MAX_TIMEOUT = 21600000

ADVICE = (
    "门铃要单独一条后台命令；重挂是三条（监听器与两个后备闹钟），各自单独一条：\n"
    "    command: cd <项目根> && shiftboss bell --once --ignore-green --heartbeat-minutes 50\n"
    "    command: cd <项目根> && shiftboss bell --alarm-minutes 100\n"
    "    command: cd <项目根> && shiftboss bell --alarm-minutes 150\n"
    f"    run_in_background: true，timeout: {MAX_TIMEOUT}\n"
    "不要折进带 &、;、|、重定向的复合命令，也不要用默认时限。"
)


def offending(tool_input: dict) -> str | None:
    command = str(tool_input.get("command", ""))
    if not BELL.search(command) or "--help" in command:
        return None
    if not STANDALONE.match(command):
        return "门铃折进了复合命令"
    if not tool_input.get("run_in_background"):
        return "门铃没有以后台命令起"
    if int(tool_input.get("timeout") or 0) < MAX_TIMEOUT:
        return "门铃的后台时限不是上限"
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
