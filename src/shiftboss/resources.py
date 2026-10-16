"""重资源名额：整机同时只跑有限条吃 CPU 与内存的检查，执行方会话数另由配额管。

开发线写代码几乎不占资源，会话可以开满配额；快速检查（生成输入闭包等）与浏览器验收才吃资源，
同时跑多了会让检查超时、页面装不出来，白跑一轮。所以只给重活限量，满额排队，不限会话数。

名额是控制目录下 `heavy-slots/<n>.lock` 的文件锁，进程退出即由系统释放；锁文件里记持有者，供状态摘要显示。
上限取 `shiftboss.toml` 的 `[resources] heavy_limit`（缺省 3）。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from shiftboss.config import _get, control_dir

DEFAULT_LIMIT = 3


def limit() -> int:
    return int(_get("resources", "heavy_limit", DEFAULT_LIMIT))


def slots_dir(repo: Path) -> Path:
    from shiftboss import inbox
    path = control_dir(inbox.origin_git_dir(repo)) / "heavy-slots"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def heavy_slot(repo: Path, what: str):
    """取得一个重资源名额再执行；满额时每 5 秒重试并打印排队状态。"""
    directory, cap = slots_dir(repo), limit()
    waited = False
    while True:
        for index in range(1, cap + 1):
            handle = open(directory / f"{index}.lock", "a+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"what": what, "where": str(repo), "pid": os.getpid(),
                                     "since": time.time()}, ensure_ascii=False))
            handle.flush()
            if waited:
                print(f"取得重资源名额 {index}/{cap}", flush=True)
            try:
                yield index
            finally:
                handle.seek(0)
                handle.truncate()
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            return
        if not waited:
            print(f"重资源名额已满（{cap}），排队等待……", flush=True)
            waited = True
        time.sleep(5)


def holders(repo: Path) -> list[dict]:
    """当前持有名额的进程：锁仍被占用且记有持有者的那些。"""
    found = []
    directory = slots_dir(repo)
    for index in range(1, limit() + 1):
        path = directory / f"{index}.lock"
        if not path.exists():
            continue
        with open(path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.seek(0)
                text = handle.read().strip()
                found.append({"slot": index, **(json.loads(text) if text else {})})
                continue
            fcntl.flock(handle, fcntl.LOCK_UN)
    return found
