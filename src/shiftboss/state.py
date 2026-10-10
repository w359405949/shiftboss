"""工作流状态的读写。**这一层就是那个「db」**：几个小 JSON，没有数据库。

**只放算不出来的东西**——我核到哪了（ack 游标）、会话号、返工计数。
凡是能从 git refs、任务书表头、副本 HEAD 现算出来的，一律不存：
存下来的派生值就是一份带时间戳的过期断言，而两个来源不一致时，
没有任何检查会告诉你该信哪个。

那几个小 JSON 的读写
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def write_state(path: Path, data: dict) -> None:
    """写工作流状态文件。**先写临时文件再原子改名,不要直接 write_text。**

    `write_text` 是「先截断再写」，中途断掉就留下半个文件；而它下次被
    `read_state` 读到时若当成空处理，状态就被静默吃掉——最坏的一种坏法。
    `os.replace` 在同一卷上是原子的，坏也只坏在 `.tmp` 上，本体始终是完整的旧版。

    **只放算不出来的**：ack 游标、会话号、返工计数。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8", newline="\n")
    _replace_with_retry(tmp, path)


# Windows 上 `os.replace` 会被**并发的读者**顶掉：目标文件只要还被谁开着，
# 就是 `PermissionError: [WinError 5]`，而这里的读者全是合法的——收件箱、
# 常驻验证的等待循环每 100ms 读一次 `index.json`。POSIX 上没这回事。
# **不要靠「读者少」来回避**：2026-09-09 常驻验证改成盯 tip 之后写得更密，
# 这一撞就把 `test_watch_red_attribution_and_claim` 打红了，
# 而现场看到的是 watch 进程整个退出——一次瞬时的共享冲突不该弄死后台验证。
# 短暂重试，超过窗口仍失败就照旧抛：那说明不是瞬时冲突。
_REPLACE_RETRY_SECONDS = 2.0


def _replace_with_retry(tmp: Path, path: Path) -> None:
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def read_state(path: Path) -> dict:
    """不存在就是空；**读不动就报错退出，绝不当成空。**

    把「文件坏了」和「还没有状态」混成一件事，后果是静默重置——ack 游标归零会让
    核过的活重新冒出来，会话台账归零会让返工计数清零、每份都重开会话。
    两者都不会有任何提示，所以这里宁可停下。

    读不动就报错停下，绝不当成空
    """
    if not path.exists():
        return {}
    # 读撞上另一进程的 os.replace 时 Windows 同样短暂报 PermissionError，与写侧同一窗口重试。
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:
            if time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            error = exc
        except (OSError, ValueError) as exc:
            error = exc
        raise SystemExit(
            f"{path} 读不动（{error}）。这是工作流状态，不许当空处理——"
            f"当空会静默重置游标/计数。先看它是不是写坏了，同目录的 .tmp 可能是好的。")


DISPATCHER_NAME = "dispatcher.json"


def claim_dispatcher(session_id: str) -> None:
    """记下用户最后对话的会话：它就是派活方。由 UserPromptSubmit 钩子调用。"""
    from shiftboss.config import control_dir
    from shiftboss.inbox import origin_git_dir
    from shiftboss.paths import project_root
    write_state(control_dir(origin_git_dir(project_root())) / DISPATCHER_NAME,
                {"session": session_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})


def require_dispatcher() -> None:
    """派活方是用户最后对话的那个会话，其余会话的派活命令一律拒绝。

    门铃重挂时无条件挤掉别的监听器；被挤掉的会话醒来会照常重挂，只有这里能让它停手。
    不在 Claude Code 会话里（没有 `CLAUDE_CODE_SESSION_ID`）的调用是用户自己跑的，不拦。
    """
    from shiftboss.config import control_dir
    from shiftboss.inbox import origin_git_dir
    from shiftboss.paths import project_root
    me = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not me:
        return
    try:
        owner = read_state(control_dir(origin_git_dir(project_root())) / DISPATCHER_NAME).get("session")
    except SystemExit:  # 没有 origin 就没有家目录，也就没有登记的派活方
        return
    if owner and me != owner:
        raise SystemExit(
            f"拒绝：本会话不是派活方——用户最后对话的是会话 {owner}。\n"
            "退出派活流程：不重挂门铃、不派活、不合并、不接回。用户在本会话发话后自动接手；"
            "发话后仍被拒，说明本会话启动时还没有认领钩子，请用户重开本会话。")


QUOTA_NAME = "quota.json"


def validate_quota(value: int) -> int:
    """负数直接报错。

    **`0` ＝ 不给这个执行方分配，不是「不限」**（2026-09-21 用户定：
    机器资源与额度都是有限的，没有「不限」这回事；要用就显式分配一个数）。
    """
    if type(value) is not int or value < 0:
        raise ValueError("配额必须是非负整数（0＝不分配）")
    return value


def read_quota(git_dir: Path, executor: str = "codex") -> int:
    """配额与目标副本数共用这一份状态，按执行方分开。

    **未设置与 `0` 都表示不分配**：要用哪个执行方就显式给它一个数。
    """
    from shiftboss.config import control_dir
    return validate_quota(read_state(control_dir(git_dir) / QUOTA_NAME).get(executor, 0))


def write_quota(git_dir: Path, value: int, executor: str = "codex") -> None:
    """只由副本准备入口在全部成功后调用，不另存副本数量。

    **配额就是该执行方的目标副本数，每个执行方只存一个数。**
    """
    from shiftboss.config import control_dir
    path = control_dir(git_dir) / QUOTA_NAME
    data = read_state(path)
    data[executor] = validate_quota(value)
    write_state(path, data)


# ---------------------------------------------------------------------------
# 合并在飞的锁
#
# 合并没提交完的时候，同一个工作副本里任何**别的** `git commit` 都会把这次合并
# 一起提交掉：提交的是整个索引，不是你 `git add` 的那几个路径，而 `MERGE_HEAD`
# 还在，于是那次提交直接变成合并提交——闸一步没跑，回显却一切正常。
#
# 认这把锁的是 `pre-commit` hook（源码在 `shiftboss.hooks.pre_commit`，由 bootstrap 装）。
# **两条路都要下锁**：`shiftboss.merge` 一进门就下；手工恢复那条路
# （`git merge --no-commit` ＋ `shiftboss.regen` ＋ `git commit`）由 regen 在
# 发现 `MERGE_HEAD` 时下——否则手工路上照样能被一次无关提交毁掉。

LOCK_NAME = "shiftboss-merge-in-flight"
ENV_BYPASS = "MM_WORKFLOW_MERGE"


def lock_path(repo: Path) -> Path:
    return repo / ".git" / LOCK_NAME


def acquire_merge_lock(repo: Path, why: str) -> None:
    """占住这个工作副本，并让本进程自己的提交能过 hook。"""
    import os
    import time
    path = lock_path(repo)
    if path.exists():
        raise SystemExit(
            f"这个工作副本里已经有一次合并在飞：{path}\n"
            f"内容：{path.read_text(encoding='utf-8', errors='replace').strip()}\n"
            "等它结束，或确认那个进程已经死了再删掉这个文件。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"pid={os.getpid()} {why} started={time.strftime('%Y-%m-%dT%H:%M:%S')}\n",
        encoding="utf-8")
    os.environ[ENV_BYPASS] = "1"


def release_merge_lock(repo: Path) -> None:
    import os
    lock_path(repo).unlink(missing_ok=True)
    os.environ.pop(ENV_BYPASS, None)
