#!/usr/bin/env python3
"""断电或崩溃之后：把当时还在执行的那几条线接回来。**只做这一件事。**

    shiftboss recover --check     # 只报告，不动手
    shiftboss recover             # 逐条 resume

**收拾现场是执行方的活，不是派活方的。** `resume` 带着完整历史回来：它自己看得见
工作树、看得见做到哪一步，也会自己查断电写坏的文件（指纹是整个文件变成 NUL 而
大小没变）并重新生成。派活方这一侧再扫一遍、再替它 `checkout` 一遍，既慢又多余，
还多一次手抖把它没提交的活冲掉的机会。2026-09-05 断电后由用户定死这条策略。

**接哪几条，只按收件箱判，不看进程、也不整份台账照单全收：**

**会话死活与分支上有没有未合并的推送是两回事**，接不接只看前者（`session_state`）：

- `在干活`——会话没停在等输入，断电打断了它，**接**。
- `停着`、`做完了没推`、`--close` 过的——会话已停在等输入或已收摊，**不接**：
  等的是核对与下一轮派活，resume 一个已经收工的会话只会让它对着干净的树发呆，
  还占住额度。

2026-09-05 那次实测：六条非 `closed` 的线里只有三条真在执行，另外三条早已推完在等核；
整份台账照单 resume 之后，那三个发呆的标签页是用户手动关掉的。上面四档就是那次的结论。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import subprocess
import sys
from pathlib import Path

REPO = project_root()

def still_running(state: str, mode: str | None) -> bool:
    """断电时这条线是不是确实还在执行：会话没停在等输入就是。`mode` 不再参与判断。"""
    return state == "在干活"


def running_lines(repo: Path) -> list[tuple[str, str]]:
    """→ [(任务书名, 它当时的状态)]，只含断电时还在执行的。

    状态用的就是收件箱那一套（`session_state`）：会话是否停在等输入、
    台账里的派出时刻对分支最后一次 push。**不要在这里另造判据**——
    再造一套就会和 `inbox list` 给出不同的答案，而那两个答案没人说得清哪个算数。

    接哪几条按 `still_running` 判，**推完在等核的不接**
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
    """**断电/崩溃之后**：把当时还在执行的那几条线接回来

    **只做这一件事**——收拾现场（查断电写坏的文件、决定哪些重做）是执行方的活，resume 带着历史回来它自己看得见；
    """
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只报告，不动手")
    args = ap.parse_args(argv)

    lines = running_lines(REPO)
    print(f"断电恢复：{REPO.parent}{'（只检查）' if args.check else ''}\n")
    if not lines:
        print("断电时没有正在执行的线，没什么可接回的。")
        print("已经推完在等核的那些不算——它们等的是核对与下一轮派活，不是 resume。")
        return 0

    print(f"断电时还在执行的 {len(lines)} 条：")
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
