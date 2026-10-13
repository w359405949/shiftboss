"""门铃：写的、装的、听的都在这一处。

- 写：执行方 push 由 bare 仓库的 `post-receive`（`HOOK`）写一行；常驻验证经 `ring()` 写一行。
- 装：`install_hook()`；文件名只经 `bell_path()` 取。
- 听：本模块的命令行；执行方一 push 就打印一行，顺带安静两小时敲一次心跳。

一次 push 一行。**这里跟那个 hook 守同一条规矩：不做判断。** 只报「哪条分支动了」
「该查一次了」，做完没做完由 `shiftboss.inbox list` 从分支上的任务书现算。
漏一响只影响快慢，不影响对错——refs 本身才是那份持久事件日志。

从**当前文件末尾**开始盯，不回放历史：历史该用 `shiftboss.inbox list` 看，
那条路不依赖任何会话记得什么。

用法：

    shiftboss bell
    shiftboss bell --path <门铃文件> --heartbeat-minutes 0
"""

from __future__ import annotations
from shiftboss.paths import project_root

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

# **盯一个文件用轮询，不用目录变更通知。** 一次 `os.stat` 是几十微秒，一秒一次
# 一天不到四秒 CPU；而变更通知那条路会在缓冲区溢出或句柄失效后静默作废——
# 「以为在听、其实早聋了」是这里最坏的坏法，因为它和「没人 push」长得一模一样。
POLL_SECONDS = 1.0
MODULE = "shiftboss.doorbell"
# 两种起法都算监听器：`python -m shiftboss.doorbell` 与 `shiftboss bell` 命令。
LISTENER_MODULES = (MODULE,)
BELL_NAME = "bell.log"


HOOK = r'''"""门铃：执行方 push 完在这里敲一下，好让活着的会话立刻知道，而不用等轮询。

**这个 hook 不许做判断。** 「做完没有」由 `shiftboss.inbox` 从分支上的任务书现算——
refs 本身就是持久事件日志，门铃丢了只影响快慢，不影响对错。所以这里只记
「哪条分支动了」，且**无论如何 exit 0**：通知的 bug 绝不能挡住交付。
"""

import os
import sys
from datetime import datetime, timezone

try:
    bell = BELL_PATH
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = []
    for row in sys.stdin:
        parts = row.split()
        if len(parts) != 3:
            continue
        _old, new, ref = parts
        if not ref.startswith("refs/heads/codex/") or set(new) == {"0"}:
            continue
        lines.append(f"{stamp} {ref[len('refs/heads/'):]}\n")
    if lines:
        with open(bell, "a", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
except BaseException:
    pass
'''


def install_hook(git_dir: Path) -> None:
    """把门铃装进 origin。hook 不进 git，所以装它的动作要是代码，不能是备忘。

    听这个门铃的是 `shiftboss.doorbell`。
    **派的是 TUI，执行方进程不会自己退出**，所以 `push` 才是那个要盯的时间点。
    """
    path = git_dir / "hooks" / "post-receive"
    from shiftboss.bootstrap import python_hook, write_hook
    # 控制目录可由 shiftboss.toml 改到别处，hook 不读配置，所以装的时候把门铃的绝对路径写进去。
    write_hook(python_hook(path, HOOK.replace("BELL_PATH", repr(str(bell_path(git_dir))))))
    print(f"门铃装好：{path}")


def ring(git_dir: Path, message: str) -> None:
    """常驻验证敲门铃：与 hook 写同一个文件，同一种读法。"""
    with bell_path(git_dir).open("a", encoding="utf-8") as handle:
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        handle.write(f"{stamp} verification {message}\n")


def bell_path(git_dir: Path) -> Path:
    """门铃文件：写的（hook 与常驻验证）与听的都只经这一处取名。"""
    from shiftboss.config import control_dir
    return control_dir(Path(git_dir)) / BELL_NAME


def default_path() -> Path:
    """门铃在 origin 里。**位置从仓库配置读，不写死**——改目录层级时写死的那份会指到
    仓库外面，返回空，然后在第一次用到它的地方才报错。"""
    repo = project_root()
    from shiftboss.paths import origin_path
    path = origin_path(repo)
    if path is None:
        raise SystemExit(f"在 {repo} 读不到本机 remote.origin，不知道去哪儿盯")
    return bell_path(path)


def watched_path(proc: psutil.Process, fallback: Path) -> Path | None:
    """另一个监听器盯的是哪个门铃：命令行里给了 `--path` 就用那个，没给就是默认那个。"""
    try:
        argv = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    for flag in ("--path", "-Path"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return Path(argv[i + 1])
    return fallback


def rival_listeners(path: Path, fallback: Path) -> list[psutil.Process]:
    """**只挤掉盯同一个门铃的。** 判据是它盯的那个文件，不是命令行里有没有
    `doorbell` 这个词——按词判会让临时 `--path` 起的监听器把生产那个一起杀掉，
    而它们盯的是另一个文件、另一把锁，根本不冲突。

    监听器是参数恰为模块名或脚本路径的那个进程；把整条命令串成一个参数的外壳（含本进程的祖先）不算。"""
    # 本进程与它的祖先都不算：`shiftboss.exe bell` 启动器是本监听器的父进程，
    # 命令行同样形如监听器，杀了它等于新门铃自杀（任务以 15 退出）。
    me = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    out = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.pid in me:
            continue
        argv = proc.info.get("cmdline") or []
        names = [a.replace("\\", "/") for a in argv]
        is_listener = (any(a in LISTENER_MODULES or a.endswith("shiftboss/doorbell.py")
                           for a in names)
                       or (len(names) > 1 and any(a.endswith(("shiftboss", "shiftboss.exe", "shiftboss-script.py")) for a in names)
                           and "bell" in names))
        if not is_listener or "--alarm-minutes" in names:
            continue
        theirs = watched_path(proc, fallback)
        if theirs and _same_file(theirs, path):
            out.append(proc)
    return out


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def listening(path: Path | None = None) -> bool:
    """这一刻有没有监听器盯着这个门铃。

    **判据与挤锁那条完全相同**（`rival_listeners`：盯的是哪个文件，不是命令行里有没有
    `doorbell` 这个词），免得两处各自判活、答案还不一样。
    """
    fallback = default_path()
    try:
        return bool(rival_listeners(path or fallback, fallback))
    except Exception:
        return True          # 判不了就别报警，宁可不说也不误报


def lock_nowait(handle) -> None:
    """不等待地独占锁住文件，抢不到抛 OSError；锁随进程释放。"""
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire(lock_path: Path, path: Path, fallback: Path):
    """独占锁：**后来的挤掉先来的。**

    同一时刻只许一个监听器，否则一次 push 会唤醒好几个会话，它们各自去核对、
    各自去合并同一份活。

    **为什么是后来者赢：** 锁随进程释放，前提是持锁进程跟着会话一起死。会话被强行
    拆掉时它会活下来，于是锁被一个没人管的孤儿占着，新会话永远挂不上门铃。正常情况下
    先起的那个更可能已经是孤儿——真正还开着的会话不会去重挂门铃。所以抢不到就把占着的
    杀掉再抢。代价是万一那个会话真的还开着，它会失去门铃，**那只影响快慢**：
    它开工跑的 `shiftboss.inbox list` 不依赖门铃。
    """
    for attempt in (1, 2):
        handle = open(lock_path, "a+b")
        try:
            lock_nowait(handle)
            return handle
        except OSError:
            handle.close()
            if attempt == 2:
                say("杀掉占锁进程之后仍然抢不到门铃，这个会话不监听。")
                say("用 shiftboss.inbox list 手查，不会漏。")
                raise SystemExit(0)
            rivals = rival_listeners(path, fallback)
            for proc in rivals:
                say(f"挤掉先前的监听器：PID {proc.pid}")
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        psutil.TimeoutExpired) as err:
                    say(f"  杀不掉（{type(err).__name__}）")
            if not rivals:
                say("锁被占着但找不到监听器进程；再试一次。")
            time.sleep(0.5)
    raise SystemExit(0)


def say(text: str) -> None:
    print(text, flush=True)


# 不叫醒派活方的行：全绿的验证、红数与上一份相同的验证（上一份已响过），以及派活方自己 `verify claim` 回响的认领。
GREEN = re.compile(r"verification (?:[0-9a-f]{40} red=(?:0 |\d+ delta=0;)|claimed )")


def drain(path: Path, offset: int, ignore_green: bool = False) -> tuple[int, bool]:
    """把 `offset` 之后新长出来的字节读出来，一行一条；返回新的 offset 与响没响。

    `ignore_green` 时全绿、红数未变的验证行与认领回响照样打印，但不算响：那一行没有要派活方做的新事。
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return offset, False
    if size < offset:          # 被截断过就从头来
        offset = 0
    if size == offset:
        return offset, False
    with path.open("rb") as handle:
        handle.seek(offset)
        blob = handle.read(size - offset)
    offset += len(blob)
    rang = False
    for line in blob.decode("utf-8", "replace").splitlines():
        if line.strip():
            say(f"门铃 {line.strip()}")
            rang = rang or not (ignore_green and GREEN.search(line))
    return offset, rang


def say_stalled() -> None:
    """门铃响一次，就把**挂住的线**一并报出来。

    **心跳兜不住多线**：它只在整个门铃安静满 `--heartbeat-minutes` 才敲，
    而任何一条线推送都会把它顺延。同时跑三五条线时几乎不会有那么久的全局安静，
    于是「执行方没做完也没 push 就死在那儿」这件事永远等不到心跳——
    2026-09-21 实测漏过一次 9 小时 11 分的空转，期间别的线一直在推。

    设计本来的兜底是「门铃响了要查整个收件箱」，但那靠人记得。
    **这里把它变成响一次就自动报一次**，读不到收件箱时静默跳过，不拦门铃。
    """
    try:
        from shiftboss.inbox import origin_git_dir, stalled_lines, survey, STALE_HOURS, humanize
        repo = project_root()
        git_dir = origin_git_dir(repo)
        live = {row["name"]: row for row in survey(repo, git_dir)}
        rows = stalled_lines(repo, git_dir, live)
    except Exception:
        return
    if not rows:
        return
    say(f"⚠ 挂住超过 {STALE_HOURS:g} 小时的 {len(rows)} 条（门铃响一次报一次）：")
    for name, age, copy, signs in rows:
        say(f"    {name}（{copy}）：已 {humanize(age)}；{signs}")
    say("    逐条试派一次（`shiftboss dispatch <名字>` 会判断会话停没停），不凭状态列认定还在干。")


def _event_time(line: str) -> float | None:
    """门铃一行开头的时刻（epoch 秒）。hook 写 UTC 带 `Z`，`ring()` 与心跳写本地无时区。"""
    try:
        moment = datetime.fromisoformat(line.split(" ", 1)[0].replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.astimezone()).timestamp()


# 推荐的重挂命令只在这里写一处。**心跳必须短于宿主的后台时限**：Claude Code 的后台命令
# 默认满 30 分钟即被结束，起门铃时把时限设为 6 小时（`timeout` 21600000，工作区 settings 的
# BASH_MAX_TIMEOUT_MS 把上限抬到 6 小时）；超过时限的心跳永远等不到。
# 心跳 50 分钟：派活方的输入缓存 60 分钟过期，醒得比它早。再挂两个后备闹钟（100、150 分钟），
# 防止被叫醒却忘了重挂；每次重挂起的新闹钟结束同分钟数的旧闹钟，从头计时。
HEARTBEAT_MINUTES = 50
ALARM_MINUTES = (100, 150)
REARM_COMMANDS = (f"shiftboss bell --once --ignore-green --heartbeat-minutes {HEARTBEAT_MINUTES}",
                  *(f"shiftboss bell --alarm-minutes {minutes}" for minutes in ALARM_MINUTES))
REARM_COMMAND = "、".join(REARM_COMMANDS) + "（各自单独一条后台命令）"


def last_event(path: Path) -> float:
    """门铃文件里最后一件**会叫醒派活方**的事的时刻；全绿的验证行不算。

    **心跳按它计时，不按本进程启动时刻计时。** 派活方用 `--once` 挂铃、每响一次就重挂，
    按进程计时的话每次重挂都从零数起——安静多久都等不满；而心跳本来就是防派活方
    忙别的事，不该靠它按时重挂来维持节奏。文件里读不到时刻就退回文件的修改时间。
    """
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return time.time()
    for line in reversed(lines):
        line = line.strip()
        if not line or GREEN.search(line):
            continue
        stamp = _event_time(line)
        if stamp is not None:
            return stamp
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def mark_path(path: Path) -> Path:
    return path.with_name(path.name + ".read")


def read_mark(path: Path) -> int:
    """从上一个监听器读到的位置接着读，不从文件末尾起。

    `--once` 响一次就退出，到派活方重挂之间长出来的行没人听；从末尾起就把它们漏了。
    没有记号（第一次挂）才从末尾起。
    """
    size = path.stat().st_size if path.exists() else 0
    try:
        mark = int(mark_path(path).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return size
    return mark if 0 <= mark <= size else 0


def write_mark(path: Path, offset: int) -> None:
    try:
        mark_path(path).write_text(str(offset), encoding="ascii")
    except OSError:
        pass


def watch(path: Path, heartbeat_minutes: int, fallback: Path, once: bool = False,
          ignore_green: bool = False) -> int:
    lock_path = path.parent / "bell.lock"
    if not path.parent.exists():
        raise SystemExit(f"门铃所在目录不存在：{path.parent}")
    handle = acquire(lock_path, path, fallback)
    started = time.time()   # 闹钟由同批重挂起的新闹钟替换（同分钟数的旧闹钟自行结束），监听器不碰它们，免得并发起时误杀
    try:
        offset = read_mark(path)
        while True:
            offset, rang = drain(path, offset, ignore_green)
            write_mark(path, offset)
            if rang:
                say_stalled()
                if once:
                    return 0
            elif heartbeat_minutes > 0 and \
                    time.time() - max(last_event(path), started) >= heartbeat_minutes * 60:
                # 心跳自己也写进门铃文件：下一次心跳从这一行起算，与谁、何时重挂无关。
                with path.open("a", encoding="utf-8") as bell:
                    bell.write(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} heartbeat\n")
                offset = path.stat().st_size
                write_mark(path, offset)
                # **心跳响了必须过整个收件箱，不是抽查。** 它存在的理由是执行方
                # 既没做完也没 push 就死在那儿时门铃永远不响，而派的是 TUI，
                # 进程还在，从外面看和在干活一样。会话判活那套不稳（手工起的认不出、
                # PID 会被复用、queue 投递成功不等于被消费），它失能时唯一还成立的
                # 判据是 refs 与台账——那正是 `inbox list` 现算的东西。
                # 所以 A 副本的 push 把我叫起来时，B 副本死没死一样看得见；
                # **反过来说，这条成立的前提是门铃响了要查整个收件箱。**
                stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                say(f"心跳 {stamp} 把整个收件箱过一遍："
                    f"shiftboss inbox list（不是只看某一条线）")
                say_stalled()
                if once:
                    return 0
            time.sleep(POLL_SECONDS)
    finally:
        handle.close()


def alarm_processes(path: Path, fallback: Path) -> list[tuple[psutil.Process, int]]:
    """盯同一个门铃的后备闹钟与其分钟数。"""
    me = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    out = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        argv = proc.info.get("cmdline") or []
        if proc.pid in me or "--alarm-minutes" not in argv or "bell" not in argv:
            continue
        theirs = watched_path(proc, fallback)
        index = argv.index("--alarm-minutes")
        if theirs and _same_file(theirs, path) and index + 1 < len(argv) and argv[index + 1].isdigit():
            out.append((proc, int(argv[index + 1])))
    return out


def stop_alarms(path: Path, fallback: Path, *, before: float, minutes: int | None = None) -> None:
    """结束早于 `before` 起的闹钟（给了 `minutes` 只结束同分钟数的）：重挂即重新计时。"""
    for proc, their_minutes in alarm_processes(path, fallback):
        if minutes is not None and their_minutes != minutes:
            continue
        try:
            if proc.create_time() < before:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def alarm(path: Path, fallback: Path, minutes: int) -> int:
    """后备闹钟：满 `minutes` 分钟仍未被重挂结束，就叫醒派活方。"""
    stop_alarms(path, fallback, before=psutil.Process().create_time(), minutes=minutes)
    time.sleep(minutes * 60)
    say(f"门铃后备闹钟：距上次重挂已 {minutes} 分钟仍未重挂。先重挂门铃（{REARM_COMMAND}），"
        "再把整个收件箱过一遍：shiftboss inbox list")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", type=Path, default=None,
                        help="门铃文件；默认取控制目录里的 bell.log")
    parser.add_argument("--heartbeat-minutes", type=int, default=HEARTBEAT_MINUTES,
                        help="门铃文件里最后一件事（全绿验证不算）过去这么久就敲一次心跳，心跳本身也记进门铃；0 表示只听门铃")
    parser.add_argument("--once", action="store_true",
                        help="响一次（门铃或心跳）就退出，供后台等待一次事件")
    parser.add_argument("--ignore-green", action="store_true",
                        help="全绿的验证行照样打印但不算响，不叫醒派活方")
    parser.add_argument("--alarm-minutes", type=int, default=None,
                        help="后备闹钟：这么多分钟后叫醒派活方；下一次重挂会结束它")
    args = parser.parse_args(argv)
    from shiftboss.state import require_dispatcher
    require_dispatcher()
    fallback = default_path()
    if args.alarm_minutes is not None:
        return alarm(args.path or fallback, fallback, args.alarm_minutes)
    try:
        return watch(args.path or fallback, args.heartbeat_minutes, fallback, args.once,
                     args.ignore_green)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
