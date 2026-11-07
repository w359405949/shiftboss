"""推送前自查：与合并时同一套快速检查（`shiftboss.merge.fast_checks`），比对分叉点与当前工作树；
不计算生成闭包、不索取生成回执；派生物生成与全量验证归常驻验证。

执行方推送前运行；这里过了，合并就不会因同源快速检查退回（README 4.1 第 10 条）。
"""
from __future__ import annotations

import argparse
import sys

from shiftboss.merge import MergeError, fast_checks, git, REPO


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="比对的上游（缺省 origin/main；先自行 fetch）")
    ap.add_argument("--task", help="任务书名；默认按当前分支识别")
    ap.add_argument("--list", action="store_true", help="只显示本次快检计划，不执行检查")
    args = ap.parse_args(argv)
    try:
        base = git("merge-base", "HEAD", args.base).stdout.strip()
        if args.list:
            from shiftboss.task_checks import describe
            paths = git("diff", "--name-only", "--no-renames", "-z", base).stdout.split("\0")
            print(describe(REPO, args.task, paths))
            return 0
        if args.task:
            fast_checks(base, task=args.task)
        else:
            fast_checks(base)
    except (MergeError, ValueError) as exc:
        print(f"[检查失败] {exc}", file=sys.stderr)
        return 1
    print("快速检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
