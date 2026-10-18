"""每次回复结束时把 `shiftboss brief` 的摘要显示给用户（钩子输出的 systemMessage）。

用户要求每次总结都能直接看到在办任务与门铃状态，用来与后台实际情况对照；由钩子现算现显示，不经派活方转述。
摘要首行「该你处理」不空时拦下收尾，把该行交回派活方先处理；同一组事项只拦一次，避免处理不了的事项反复拦。
钩子自身出错时不显示，不挡回复。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys

PREFIX = "【该你处理】"


def main() -> int:
    json.load(sys.stdin)
    from shiftboss import brief, config
    from shiftboss.paths import project_root
    from shiftboss.state import read_state, write_state
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        brief.main([])
    text = out.getvalue().rstrip()
    result = {"systemMessage": text}
    line = next((row for row in text.splitlines() if row.startswith(PREFIX)), "")
    items = line.removeprefix(PREFIX)
    if items and items != "：无":
        store = config.control_dir(brief.inbox.origin_git_dir(project_root())) / "stop-block.json"
        digest = hashlib.sha256(items.encode("utf-8")).hexdigest()
        if (read_state(store) or {}).get("digest") != digest:
            write_state(store, {"digest": digest})
            result.update(decision="block", reason=f"收尾前先处理简报「该你处理」：{items}。逐项处理，或说明为何暂不处理后再收尾。")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
