"""派活方判「这条线在不在干活」时去翻 codex 进程或会话 rollout —— 这个钩子拦住它。

**规矩本身写在 [`shiftboss.inbox.session_state`](../inbox.py) 上方**，那里说清了为什么：
进程和 rollout 是「看会话那一侧」，它连「派出去还没开始推」和「跑完停着」都分不开，
而「分支上有没有未合并的 push」分得开。这里只做一件事——**让违反它的命令失败**。

**为什么需要一个会失败的检查，而不是再写一条备忘**：那条规矩 2026-09-02 违反过一次，
2026-09-09 又违反了一次（当天 `shiftboss/README.md` 第 3.1 节还在教人去看 rollout 时间戳，
那句已经删掉）。备忘拦不住下一个会话，一条会失败的检查能。

**留了一个显式出口**：命令里带 `WORKFLOW_INFRA_PROBE` 就放行。查基础设施故障
（会话被手工 `codex resume` 起过、沙箱掉级）是真的要看进程和 rollout 的，
但那要写在命令里让人看见，不能顺手当成判工作状态的路子。
"""

from __future__ import annotations

import json
import re
import sys

# 「查 codex 进程」＝ 列进程的动作 ＋ codex 出现在同一条命令里。
# 两个条件都要，否则 `shiftboss dispatch ...` 这种正常命令会被误伤。
PROCESS_LISTERS = re.compile(
    r"\b(get-process|get-ciminstance|get-wmiobject|tasklist|pgrep|\bps\b)\b", re.I)
CODEX = re.compile(r"codex(\.exe)?\b", re.I)

# 会话 rollout：直接读那些 jsonl，或者去翻 ~/.codex/sessions 目录。
ROLLOUT = re.compile(r"rollout-[^\s\"']*\.jsonl|[.]codex[\\/]+sessions", re.I)

ESCAPE = "WORKFLOW_INFRA_PROBE"

ADVICE = (
    "这条命令在用 codex 进程 / 会话 rollout 判执行方的工作状态。**那不是判据。**\n"
    "唯一的来源是收件箱：\n"
    "    shiftboss inbox list      # 执行状态：在干活 / 做完了没推 / 停着；分支：可核对 / 进行中 / 未推分支\n"
    "    shiftboss inbox status\n"
    "理由写在 shiftboss.inbox 的 session_state 上方：进程与 rollout 是「看会话那一侧」，\n"
    "这些读取收在 inbox 一处；会话死活与分支上的推送在收件箱里分两列报。\n"
    f"确实是在查基础设施故障（手工 resume、沙箱掉级）就把 {ESCAPE} 写进命令里，那样放行。"
)


def offending(command: str) -> str | None:
    if ESCAPE in command:
        return None
    if PROCESS_LISTERS.search(command) and CODEX.search(command):
        return "查 codex 进程"
    if ROLLOUT.search(command):
        return "翻会话 rollout"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # 读不懂就别挡路：这个钩子的作用是提醒，不是守门。
    command = str((payload.get("tool_input") or {}).get("command", ""))
    hit = offending(command)
    if hit is None:
        return 0
    print(f"{hit}：这条被工作流拦下了。\n{ADVICE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
