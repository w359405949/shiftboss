"""每次回复结束时把 `shiftboss brief` 的摘要显示给用户（钩子输出的 systemMessage）。

用户要求每次总结都能直接看到在办任务与门铃状态，用来与后台实际情况对照；由钩子现算现显示，不经派活方转述。
钩子自身出错时不显示，不挡回复。
"""
from __future__ import annotations

import contextlib
import io
import json
import sys


def main() -> int:
    json.load(sys.stdin)
    from shiftboss import brief
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        brief.main([])
    print(json.dumps({"systemMessage": out.getvalue().rstrip()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
