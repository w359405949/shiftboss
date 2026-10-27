"""shiftboss 包仓库不 pull、不 push —— 这个钩子拦住落在包仓库里的 `git push`／`git pull`。

**为什么**：包仓库有 GitHub 远端，按约定不自动推送；派活方曾四次把改包仓库与改项目仓库的命令
放进同一批并行调用，共享的工作目录串到包仓库，把提交推上 GitHub、把 origin 换成本机 hub。
备忘拦不住。命令里 `cd` 进包仓库，或当前目录就在包仓库里，都算落在包仓库。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import shiftboss

PACKAGE_REPO = Path(shiftboss.__file__).resolve().parents[2]
SYNC = re.compile(r"(?:^|[;&|({\n]|\b(?:do|then|else))\s*(?P<git>git)\s+(?:-C\s+(\S+)\s+)?(push|pull)\b")
CD = re.compile(r"(?:^|[;&|({\n])\s*cd\s+([^;&|\n]+?)\s*(?=&&|;|\||$)")


def inside(path: str, base: Path) -> bool:
    try:
        Path(os.path.expanduser(path.strip("'\""))).resolve().relative_to(base)
        return True
    except ValueError:
        return False


def offending(tool_input: dict, cwd: str) -> str | None:
    command = str(tool_input.get("command", ""))
    for match in SYNC.finditer(command):
        before = command[:match.start("git")]
        dirs = [m.group(1) for m in CD.finditer(before)]
        target = match.group(2) or (dirs[-1] if dirs else cwd)
        if target and inside(target, PACKAGE_REPO):
            return f"`git {match.group(3)}` 落在 shiftboss 包仓库（{PACKAGE_REPO}）"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    hit = offending(payload.get("tool_input") or {}, str(payload.get("cwd") or os.getcwd()))
    if hit is None:
        return 0
    print(f"{hit}：这条被工作流拦下了。\n包仓库只本地提交，推送 GitHub 由用户决定；改包与改项目的命令分开发，等上一条返回再发下一条。",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
