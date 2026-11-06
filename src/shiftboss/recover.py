#!/usr/bin/env python3
"""把该在执行但进程退出或执行中断的那几条线接回来；常驻验证留下旧锁而进程没了时一并重起。

    shiftboss recover --check     # 只报告，不动手
    shiftboss recover             # 逐条 resume

**收拾现场是执行方的活，不是派活方的。** `resume` 带着完整历史回来：它自己看得见
工作树、看得见做到哪一步，也会自己查断电写坏的文件（指纹是整个文件变成 NUL 而
大小没变）并重新生成。派活方这一侧再扫一遍、再替它 `checkout` 一遍，既慢又多余，
还多一次手抖把它没提交的活冲掉的机会。2026-09-05 断电后由用户定死这条策略。

**接哪几条只按收件箱的执行状态（`session_state`）判：**

- `进程已退出`——会话没停在等输入、副本上却没有执行方进程，**接**。
- `执行中断`——插件确认最新一轮以结构化错误结束，即使终端仍活着也接；
- `在干活`——没有已知中断且进程还在，**不接**：`--recover` 会往活会话里投「继续」，打断正在做的事。
- `停着`、`做完了没推`、`--close` 过的——等的是核对与下一轮派活，**不接**：
  resume 一个已经收工的会话只会让它对着干净的树发呆，还占住额度。

所以随时可以跑、跑几次都一样：派活方会话被中断后恢复时由钩子跑它，相当于多敲一次心跳；
重启机器与重启编辑器不必区分。
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
from contextlib import contextmanager
import subprocess
import sys
from pathlib import Path

REPO = project_root()


@contextmanager
def recovery_lock(repo: Path):
    """手工入口与事件入口共用进程锁；崩溃自动释放，不以遗留文件判忙。"""
    from shiftboss.config import control_dir
    from shiftboss.inbox import origin_git_dir
    from shiftboss.doorbell import lock_nowait
    directory = control_dir(origin_git_dir(repo), repo)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "recovery.lock").open("a+b") as handle:
        try:
            lock_nowait(handle)
        except OSError:
            yield False
        else:
            yield True

def still_running(state: str, mode: str | None) -> bool:
    """这条线该在执行但进程退出或执行中断；`mode` 不参与判断。"""
    return state in ("进程已退出", "执行中断")


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
        if live.get(name, {}).get("state") == "可核对":
            continue  # 完成/停下的交回已到，等派活方处理，不因尾部错误自动重派。
        if still_running(state, mode):
            out.append((name, state))
    return out


def restart_resident(check: bool) -> None:
    """常驻验证与常驻巡检：旧锁在、进程没了＝重启前在跑、被中断；没有锁的不起（那是有意停的）。"""
    import psutil
    from shiftboss import config, native, patrol, verify
    remote = verify.origin(REPO)
    for title, directory, lock_name, stop_name, key, module, command, workspace in (
            ("常驻验证", verify.state_dir(remote), "worker.lock", "stop", "verify", "shiftboss.verify", "watch", "verify"),
            ("常驻巡检", patrol.state_dir(remote), "worker.lock", "stop", "patrol", "shiftboss.patrol", "watch", patrol.NAME),
            ("定时支线", patrol.state_dir(remote), "periodic.lock", "periodic-stop", "periodic", "shiftboss.patrol", "periodic", patrol.NAME)):
        lock = directory / lock_name
        if not lock.exists() or (directory / stop_name).exists() or not config.automation(key, REPO):
            continue
        holder = lock.read_text(encoding="utf-8").strip()
        if holder.isdigit() and psutil.pid_exists(int(holder)):
            continue
        print(f"{title} PID {holder or '无'} 已不在{'（只检查）' if check else '，重起'}")
        if not check:
            # 每次只起意外退出的这一条，不能借 start 顺带复活有意停止的兄弟支线。
            argv = [sys.executable, "-B", "-m", module, command, "--repo", str(REPO),
                    "--workspace", str(config.copies_dir(remote) / workspace)]
            if key == "verify":
                argv.append("--publish")
            print(f"{title} 已起：PID {native.spawn(argv, str(REPO))}")


def main(argv: list[str] | None = None) -> int:
    """把该在执行但进程退出或执行中断的那几条线接回来；收拾现场是执行方的活。"""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只报告，不动手")
    args = ap.parse_args(argv)
    from shiftboss.state import require_dispatcher
    require_dispatcher()
    from shiftboss import config
    if not args.check and not config.automation("session_recover", REPO):
        print("自动任务「会话恢复」已关（shiftboss.toml [automation] session_recover），只报告不动手")
        args.check = True

    if args.check:
        return run(True)
    with recovery_lock(REPO) as acquired:
        if not acquired:
            print("已有恢复任务在执行，本次不重复启动。")
            return 0
        return run(False)


def run(check: bool, output=None) -> int:
    """调用方已取得恢复锁；所有入口复用同一收件箱判据。"""
    restart_resident(check)
    lines = running_lines(REPO)
    if not lines:
        print(f"shiftboss recover：{REPO.parent} 没有该在执行但进程退出或执行中断的线。")
        return 0

    print(f"shiftboss recover：{REPO.parent}{'（只检查）' if check else ''}")
    print(f"该在执行但进程退出或执行中断的 {len(lines)} 条：")
    for name, state in lines:
        print(f"  {name:<40} {state}")
    if check:
        print("\n--check，没有接回。去掉它就逐条 resume。")
        return 0

    print()
    failed = False
    for name, _state in lines:
        result = subprocess.run([sys.executable, "-B", "-m", "shiftboss.dispatch",
                                 name, "--recover"], stdout=output, stderr=output, cwd=REPO)
        if result.returncode:
            print(f"{name} 恢复失败，退出 {result.returncode}；保留现场，不能报已恢复。", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
