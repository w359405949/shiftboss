"""推送守卫：拒收与 main 冲突的执行方分支（README 2.3 第 1 条），以及越权改动受保护路径的分支。

受保护路径与可改它们的线由 main 上的 `shiftboss.toml` `[guard]` 给出（`protected` 路径前缀、`owners` 任务书名）。

交回顶部执行状态为「停下」的照收：卡点报告须能立即推送（4.2），解不了的冲突按 2.3 第 4 条标待议交回。
守卫自身出错时放行。
"""

import subprocess
import sys
import tomllib

from shiftboss.paths import PLANS_DIR
from shiftboss.taskbook import execution_status


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)


def conflicts(new: str) -> list[str]:
    """与 main 合并冲突的路径；无冲突或判不出时为空。"""
    done = git("merge-tree", "--write-tree", "--name-only", "--no-messages", "refs/heads/main", new)
    if done.returncode != 1:
        return []
    return [line for line in done.stdout.splitlines()[1:] if line]


def guard_rules() -> tuple[list[str], set[str]]:
    """main 上的受保护路径前缀与可改它们的任务书名；读不到时不设防。"""
    shown = git("show", "refs/heads/main:shiftboss.toml")
    if shown.returncode:
        return [], set()
    guard = tomllib.loads(shown.stdout).get("guard", {})
    return list(guard.get("protected", [])), set(guard.get("owners", []))


def trespass(new: str, protected: list[str]) -> list[str]:
    """分支自与 main 分叉以来改动的受保护路径。"""
    done = git("diff", "--name-only", "--no-renames", "refs/heads/main..." + new)
    if done.returncode:
        return []
    return [path for path in done.stdout.splitlines()
            if any(path == prefix or (prefix.endswith("/") and path.startswith(prefix)) for prefix in protected)]


def main() -> int:
    protected, owners = guard_rules()
    rejected = []
    trespassed = False
    for row in sys.stdin.read().splitlines():
        parts = row.split()
        if len(parts) != 3 or not parts[2].startswith("refs/heads/codex/") or set(parts[1]) == {"0"}:
            continue
        _old, new, ref = parts
        name = ref[len("refs/heads/codex/"):]
        if protected and name not in owners and (paths := trespass(new, protected)):
            print(f"拒收 {ref}：改动了受保护路径，只有 {'、'.join(sorted(owners)) or '（未指定）'} 能改；缺测试或构建改动写进交回，由派活方转交。", file=sys.stderr)
            for path in paths:
                print(f"  {path}", file=sys.stderr)
            trespassed = True
            continue
        paths = conflicts(new)
        if not paths:
            continue
        delivery = git("show", f"{new}:{PLANS_DIR}/{name}/delivery.md").stdout
        if execution_status(delivery) == "停下":
            continue
        rejected.append((ref, paths))
    for ref, paths in rejected:
        print(f"拒收 {ref}：与 main 冲突，先 git fetch origin 并合入 origin/main 再推（README 2.3、4.1）。", file=sys.stderr)
        for path in paths:
            print(f"  {path}", file=sys.stderr)
    return 1 if rejected or trespassed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
