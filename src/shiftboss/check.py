"""推送前自查：与合并时同一套快速检查（`shiftboss.merge.fast_checks`），比对分叉点与当前工作树；
改动碰到生成输入时，未提交即失败，已提交的再查全新检出的生成核对记录（`shiftboss.merge.generation_input_reason`）。

执行方推送前运行；这里过了，合并就不会因快速检查或缺生成核对退回（README 4.1 第 10 条）。
"""
from __future__ import annotations

import argparse
import sys

from shiftboss.merge import MergeError, fast_checks, generation_input_reason, git


def uncommitted_generation_inputs() -> list[str]:
    """工作树相对 HEAD 未提交（含未跟踪）的改动里命中生成阶段输入的路径；生成核对记录只认提交。"""
    from shiftboss import regen
    stages = regen.validate_stages(regen.load_plan())
    if not stages:
        return []
    paths = sorted(set(filter(None, git("diff", "--name-only", "--no-renames", "HEAD", check=False).stdout.splitlines()
                              + git("ls-files", "--others", "--exclude-standard", check=False).stdout.splitlines())))
    chosen = regen.affected_stages(regen.closure_stages(stages), paths)
    return [p for p in paths if any(regen.matches_prefix(p, prefix) for s in chosen for prefix in s["inputs"])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="比对的上游（缺省 origin/main；先自行 fetch）")
    args = ap.parse_args(argv)
    try:
        fast_checks(git("merge-base", "HEAD", args.base).stdout.strip())
    except MergeError as exc:
        print(f"[检查失败] {exc}", file=sys.stderr)
        return 1
    if pending := uncommitted_generation_inputs():
        shown = "、".join(pending[:5]) + ("……" if len(pending) > 5 else "")
        print(f"[检查失败] 未提交的改动碰到生成输入（{shown}）：先提交，再做全新检出的生成核对，最后重跑本检查", file=sys.stderr)
        return 1
    if reason := generation_input_reason("HEAD"):
        print(f"[检查失败] {reason}", file=sys.stderr)
        return 1
    print("快速检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
