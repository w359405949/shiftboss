"""推送前自查：与合并时同一套快速检查（`shiftboss.merge.fast_checks`），比对分叉点与当前工作树；
已提交的改动碰到生成输入时，再查全新检出的生成核对记录（`shiftboss.merge.generation_input_reason`）。

执行方推送前运行；这里过了，合并就不会因快速检查或缺生成核对退回（README 4.1 第 10 条）。
"""
from __future__ import annotations

import argparse
import sys

from shiftboss.merge import MergeError, fast_checks, generation_input_reason, git


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="比对的上游（缺省 origin/main；先自行 fetch）")
    args = ap.parse_args(argv)
    try:
        fast_checks(git("merge-base", "HEAD", args.base).stdout.strip())
    except MergeError as exc:
        print(f"[检查失败] {exc}", file=sys.stderr)
        return 1
    if reason := generation_input_reason("HEAD"):
        print(f"[检查失败] {reason}", file=sys.stderr)
        return 1
    print("快速检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
