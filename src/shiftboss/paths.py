"""项目根：使用 shiftboss 的那个 git 工作树。"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


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


def same_dir(a: str | None, b: str | None) -> bool:
    """两个目录写法是否指同一处：不分大小写、不分斜杠方向、不计末尾分隔符。"""
    if not a or not b:
        return False
    return (a.replace("/", "\\").rstrip("\\").lower()
            == b.replace("/", "\\").rstrip("\\").lower())
