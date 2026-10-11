"""提交守卫：`shiftboss.merge` 在飞的时候，这个工作副本里任何**别的** git commit 都是灾难。

提交的是整个索引，不是你 `git add` 的那几个路径，而 `MERGE_HEAD` 还在，
于是那次提交直接变成合并提交——闸一步没跑完，回显却一切正常。

这是唯一能拦住它的地方。装它的代码在 `shiftboss.bootstrap`（hook 不进 git），
装的时候配一个 sh 外壳，由它以绝对路径起当前解释器。

**只在锁文件真的存在时才拦。** 自己出意外要放行——守卫把所有提交都挡住，
比它偶尔漏掉一次更糟。
"""

import os
import subprocess
import sys


def git_dir() -> str | None:
    """hook 的 cwd 是仓库顶层，但 `GIT_DIR` 不一定有；子模块与 worktree 下
    `.git` 是文件不是目录，所以问 git 而不是自己拼路径。"""
    env = os.environ.get("GIT_DIR")
    if env:
        return env
    try:
        done = subprocess.run(["git", "rev-parse", "--git-dir"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def main() -> int:
    if os.environ.get("MM_WORKFLOW_MERGE"):
        return 0
    root = git_dir()
    if not root:
        return 0
    failed = os.path.join(root, "shiftboss-merge-failed")
    if os.path.isfile(failed) and os.path.isfile(os.path.join(root, "MERGE_HEAD")):
        print("[拒绝提交] 这里是 shiftboss merge 失败后留下的合并现场；普通提交会把整份合并交掉、绕过快速检查。",
              file=sys.stderr)
        print("修好后用 shiftboss merge --finish <合并前HEAD> 提交，或 git merge --abort 撤回。", file=sys.stderr)
        return 1
    lock = os.path.join(root, "shiftboss-merge-in-flight")
    if not os.path.isfile(lock):
        return 0
    with open(lock, encoding="utf-8", errors="replace") as handle:
        body = handle.read()
    print("[拒绝提交] 这个工作副本里有一次 shiftboss.merge 在飞：", file=sys.stderr)
    for line in body.splitlines():
        print(f"  {line}", file=sys.stderr)
    print("现在提交会把那次合并一起交掉，而闸还没跑完。", file=sys.stderr)
    print(f"等它结束；确认那个进程已经死了，删掉 {lock} 再提交。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as err:            # 守卫自己出意外时放行，不挡住所有提交
        print(f"[提交守卫异常，放行] {type(err).__name__}: {err}", file=sys.stderr)
        raise SystemExit(0)
