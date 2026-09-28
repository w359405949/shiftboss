#!/usr/bin/env python3
"""把该在执行而进程没了的那几条线接回来。**只做这一件事。**

    shiftboss recover --check     # 只报告，不动手
    shiftboss recover             # 逐条 resume

**收拾现场是执行方的活，不是派活方的。** `resume` 带着完整历史回来：它自己看得见
工作树、看得见做到哪一步，也会自己查断电写坏的文件（指纹是整个文件变成 NUL 而
大小没变）并重新生成。派活方这一侧再扫一遍、再替它 `checkout` 一遍，既慢又多余，
还多一次手抖把它没提交的活冲掉的机会。2026-09-05 断电后由用户定死这条策略。

**接哪几条只按收件箱的执行状态（`session_state`）判：**

- `进程已退出`——会话没停在等输入、副本上却没有执行方进程，**接**。
- `在干活`——进程还在，**不接**：`--recover` 会往活会话里投「继续」，打断正在做的事。
- `停着`、`做完了没推`、`--close` 过的——等的是核对与下一轮派活，**不接**：
  resume 一个已经收工的会话只会让它对着干净的树发呆，还占住额度。

所以随时可以跑、跑几次都一样：派活方会话被中断后恢复时由钩子跑它，相当于多敲一次心跳；
重启机器与重启编辑器不必区分。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import subprocess
import sys
from pathlib import Path

REPO = project_root()

def still_running(state: str, mode: str | None) -> bool:
    """这条线该在执行而进程没了；`mode` 不参与判断。"""
    return state == "进程已退出"


def running_lines(repo: Path) -> list[tuple[str, str]]:
    """→ [(任务书名, 它的状态)]，只含该接回的。

    状态用的就是收件箱那一套（`session_state`）：会话是否停在等输入、
    台账里的派出时刻对分支最后一次 push。**不要在这里另造判据**——
    再造一套就会和 `inbox list` 给出不同的答案，而那两个答案没人说得清哪个算数。

    接哪几条按 `still_running` 判。
    """
    from shiftboss.dispatch import load_sessions
    from shiftboss.inbox import origin_git_dir, session_state, survey, taskbook_of

    git_dir = origin_git_dir(repo)
    live = {row["name"]: row for row in survey(repo, git_dir)}
    ledger = load_sessions()
    out = []
    for name in sorted(ledger):
        entry = ledger[name]
        if entry.get("closed"):
            continue
        state = session_state(name, live.get(name), entry, git_dir)
        # **交付模式从 `main` 读，不读分支上那份**，同 `survey` 的理由：
        # 表头是派活方写的，分支要等执行方下一次合 origin/main 才看得到。
        mode = (taskbook_of(git_dir, "main", name)[0] or {}).get("交付模式")
        if still_running(state, mode):
            out.append((name, state))
    return out


def main(argv: list[str] | None = None) -> int:
    """把该在执行而进程没了的那几条线接回来；收拾现场是执行方的活。"""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只报告，不动手")
    args = ap.parse_args(argv)

    lines = running_lines(REPO)
    if not lines:
        print(f"shiftboss recover：{REPO.parent} 没有该在执行而进程没了的线。")
        return 0

    print(f"shiftboss recover：{REPO.parent}{'（只检查）' if args.check else ''}")
    print(f"该在执行而进程没了的 {len(lines)} 条：")
    for name, state in lines:
        print(f"  {name:<40} {state}")
    if args.check:
        print("\n--check，没有接回。去掉它就逐条 resume。")
        return 0

    print()
    for name, _state in lines:
        subprocess.run([sys.executable, "-B", "-m", "shiftboss.dispatch",
                        name, "--recover"])
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
