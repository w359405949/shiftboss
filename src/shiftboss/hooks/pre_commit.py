"""提交守卫：防止提交未完成合并，并自动执行任务登记的前台快检。

提交的是整个索引，不是你 `git add` 的那几个路径，而 `MERGE_HEAD` 还在，
于是那次提交直接变成合并提交——闸一步没跑完，回显却一切正常。

这是唯一能拦住它的地方。装它的代码在 `shiftboss.bootstrap`（hook 不进 git），
装的时候配一个 sh 外壳，由它以绝对路径起当前解释器。

合并锁守卫只在锁文件存在时拦截；登记快检的非零与配置异常都拒绝提交。
按暂存路径触发本任务现有前台命令，不另建检查清单或执行后台验证。
"""

import os
import subprocess
import sys


def registered_checks() -> int:
    """按本次暂存路径触发原登记命令；普通报告不触发源码编译。"""
    try:
        changed = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
            capture_output=True, text=True, check=True)
        names = [name for name in changed.stdout.split("\0") if name]
        if not names:
            return 0
        from shiftboss.merge import run_fast_commands
        run_fast_commands(names)
    except (Exception, KeyboardInterrupt) as error:
        print(f"[拒绝提交] 登记快速检查未通过：{error}", file=sys.stderr)
        return 1
    return 0


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
        return registered_checks()
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
