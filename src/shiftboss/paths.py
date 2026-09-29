"""项目根：使用 shiftboss 的那个 git 工作树。"""
from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

PROJECT_DIR = "shiftboss"
"""shiftboss 在项目里只占这一个顶层目录：装入的工作流文档与任务书都在这里，不假定项目有 `docs/`。"""
PLANS_DIR = f"{PROJECT_DIR}/plans"


@lru_cache(maxsize=1)
def project_root() -> Path:
    """当前目录所在 git 工作树的顶层。**按当前目录认，不按包文件的位置认**：
    包可能装在 site-packages，而每个工作副本都是各自的项目根。"""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, encoding="utf-8").stdout.strip()
    if not top:
        raise SystemExit("shiftboss 要在项目的 git 工作树里运行：当前目录不在任何工作树内")
    return Path(top).resolve()


def package_dir_in(root: Path) -> str | None:
    """包的发布根（含 pyproject 的目录）在 `root` 里的相对路径；包不在项目内时为 None。"""
    try:
        return Path(__file__).resolve().parents[2].relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def origin_url(repo: Path) -> str:
    """`remote.origin.url` 原文；没配置给空串。"""
    return subprocess.run(["git", "-C", str(repo), "config", "remote.origin.url"],
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def origin_path(repo: Path) -> Path | None:
    """本机 origin 的绝对路径；相对写法按 `repo` 所在工作树的顶层解析（与 git 一致）。不是本机路径给 None。"""
    url = origin_url(repo)
    if not url or "://" in url or url.startswith("git@"):
        return None
    top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, encoding="utf-8").stdout.strip()
    return (Path(top or repo) / url).resolve()


def relative_url(target: Path, repo: Path) -> str:
    """写进 origin 的写法：相对工作树顶层、正斜杠。Windows 与 WSL 的 git 都按顶层解析，两边都认。"""
    return Path(os.path.relpath(Path(target).resolve(), Path(repo).resolve())).as_posix()


_DRIVE = re.compile(r"^([A-Za-z]):[\\/]")


def _dir_key(path: str) -> str:
    """Windows 盘符路径换成 WSL 挂载写法（`E:\\x` → `/mnt/e/x`），再统一斜杠、大小写与末尾分隔符。"""
    path = _DRIVE.sub(lambda m: f"/mnt/{m[1].lower()}/", path)
    return path.replace("\\", "/").rstrip("/").lower()


def same_dir(a: str | None, b: str | None) -> bool:
    """两个目录写法是否指同一处：Windows 盘符与 `/mnt/<盘>` 视为同一处，不分大小写、斜杠方向与末尾分隔符。"""
    if not a or not b:
        return False
    return _dir_key(a) == _dir_key(b)
