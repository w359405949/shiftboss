"""派活方去跑测试套件、生成、打包 —— 这个钩子拦住它。

**规矩写在 [`shiftboss/README.md`](../../README.md) 第 3.1 节第 5 条**：
甲方能跑的只有「这一条要成立的事」——任务书点名的定向检查、一段真实回显、
一次反证，秒到分钟量级。要浏览器、要构建、要全量的，一律归常驻验证线。

**为什么需要一条会失败的检查**：这条边界 2026-09-09 一天里被越过两次。
第一次是把浏览器合同全套当成「核对」跑了；第二次是改完 `merge.py`
顺手跑整个 `test_fast_merge`（19 条、92 秒）。
两次都不是不知道规矩，是**手比规矩快**。备忘拦不住下一个会话，一条会失败的检查能。

**放行「一条」，拦住「一片」。** 反证是甲方的本职（把新语义破坏掉，看那条断言
变不变红），所以点名到具体测试方法的调用放行；模块、目录、整个套件不放行。

**没有出口，这是故意的。** 曾经留过一个 `WORKFLOW_ACCEPT_RUN` 逃生口，
用户 2026-09-09 当天要求拆掉：**「后面你的记忆一涣散，这块的约束就形同虚设，
然后会很轻易的膨胀成几十分钟的巨无霸，任何有这种苗头的地方都要掐掉。」**
他说得对——逃生口拦不住的正是它要拦的那种人：一个会给自己找理由的下一轮会话。
一条留了后门的边界不是边界。

**唯一放行的是「点名到一条测试方法」**，因为**反证是甲方的本职**
（改回旧语义、看那条断言变不变红）。那是有界的：一条方法、一次、秒级。
**这不是「小的就放行」**——模块级不放行，哪怕它只跑 0.2 秒，
因为「小」是会长大的，而「一条方法」不会。

真要跑整套，去看常驻验证的报告；那才是跑它的地方。
"""

from __future__ import annotations

import json
import re
import sys

# 跑测试的入口。pytest 不带参数也是全量，所以它单独算。
UNITTEST = re.compile(r"-m\s+unittest\b|python[^|;]*\bunittest\b", re.I)
PYTEST = re.compile(r"\bpytest\b|-m\s+pytest\b", re.I)

# 生成 / 打包 / 启动常驻验证：这些不是甲方的活。
# **只拦「会去跑」的那些**：`shiftboss.verify status|claim|stop` 是甲方的正常动作
# （claim 就是把红和任务书关联起来，README 3.1 明写着），`regen --list` 只打印步骤。
OTHER_LINES = re.compile(
    r"(?:-m\s+shiftboss\.|\bshiftboss\s+)verify\s+(watch|--once)"
    r"|(?:-m\s+shiftboss\.|\bshiftboss\s+)regen\b(?![^|;]*--list)"
    r"|-m\s+engine\.tools\.mm_(regen|package|build)\b", re.I)

# 「点名到一条」＝ 模块.类.方法，至少三段，最后一段是 test_ 开头。
# `test_fast_merge` 只有两段，是整个模块；
# `test_fast_merge.FastMergeAcceptance.test_x` 是一条。
NAMED_ONE = re.compile(r"\b[\w.]+\.[A-Z]\w*\.test_\w+\b")
PYTEST_NAMED_ONE = re.compile(r"\S+\.py::\w+::\w+")


def offending(command: str) -> str | None:
    # **没有逃生口**：见模块开头。想加一个就是这条边界开始失效的那一刻。
    if OTHER_LINES.search(command):
        return "跑生成 / 打包 / 常驻验证"
    if UNITTEST.search(command):
        if command.count("discover") or not NAMED_ONE.search(command):
            return "跑整个测试模块或套件"
        return None
    if PYTEST.search(command) and not PYTEST_NAMED_ONE.search(command):
        return "跑整个测试模块或套件"
    return None


ADVICE = (
    "**这不是甲方的活。** 合并这条路上只做两件事：基础检查，和合并；\n"
    "测试套件、生成、打包全部归常驻验证线——换个名字（「核对一下」「确认好了」）也一样。\n"
    "甲方能跑的只有「这一条要成立的事」：任务书点名的定向检查、一段真实回显、一次反证。\n"
    "要看测试结果就去收件箱：\n"
    "    shiftboss inbox list      # 红几条、较上次增减、归谁\n"
    "反证要跑就**点名到那一条方法**（模块.类.test_方法）——整模块不行，\n"
    "哪怕它只跑 0.2 秒：小的会长大，一条方法不会。\n"
    "**这条没有逃生口**，是用户 2026-09-09 明确要求拆掉的。\n"
    "要看整套结果就看验证线的报告，那才是跑它的地方。"
)


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
