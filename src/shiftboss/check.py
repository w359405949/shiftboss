"""推送前自查：与合并时同一套快速检查（`shiftboss.merge.fast_checks`），比对分叉点与当前工作树。

执行方推送前运行；这里过了，合并就不会因快速检查退回（README 4.1 第 10 条）。
"""
from __future__ import annotations

import argparse
import sys

from shiftboss.merge import MergeError, fast_checks, git
from shiftboss.paths import project_root
from shiftboss.resources import heavy_slot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="比对的上游（缺省 origin/main；先自行 fetch）")
    args = ap.parse_args(argv)
    # 快速检查吃资源，整机限量排队（shiftboss.resources）；执行方会话本身不受此限。
    try:
        with heavy_slot(project_root(), "快速检查"):
            fast_checks(git("merge-base", "HEAD", args.base).stdout.strip())
    except MergeError as exc:
        print(f"[检查失败] {exc}", file=sys.stderr)
        return 1
    print("快速检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
