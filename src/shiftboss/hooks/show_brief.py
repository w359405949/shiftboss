"""每次回复结束时把 `shiftboss brief` 的摘要显示给用户（钩子输出的 systemMessage）。

用户要求每次总结都能直接看到在办任务与门铃状态，用来与后台实际情况对照；由钩子现算现显示，不经派活方转述。
摘要首行「该你处理」不空时拦下收尾，把该行交回派活方先处理；只在出现新事项时拦（新类别、计数变大或多出名字），
事项减少或不变不再拦：处理不了的事项与正在逐条消化的事项（如后台逐条派发）不反复拦。
钩子自身出错时不显示，不挡回复。
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys

PREFIX = "【该你处理】"


def parse(items: str) -> dict[str, list[str]]:
    """「可派 9；待办 T1、T2」→ {"可派": ["9"], "待办": ["T1", "T2"]}。"""
    parsed = {}
    for item in items.split("；"):
        label, _, rest = item.partition(" ")
        parsed[label] = rest.split("、") if rest else []
    return parsed


def grew(now: dict[str, list[str]], before: dict[str, list[str]]) -> bool:
    for label, values in now.items():
        if label not in before:
            return True
        old = before[label]
        counts = [re.match(r"\d+", value[0]) if len(value) == 1 else None for value in (values, old)]
        if all(counts):
            if int(counts[0][0]) > int(counts[1][0]):
                return True
        elif not set(values) <= set(old):
            return True
    return False


def sync_main(repo) -> None:
    """派活方工作树在 main 上且干净时先快进到 origin/main：自动合入在巡检副本里合并、推送、归档，
    本地 main 不跟上时简报按旧任务书把已归档的线算成「可派」「待收尾」。不干净或不能快进就不动。"""
    import subprocess
    git = lambda *args: subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    if git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main" or git("status", "--porcelain").stdout.strip():
        return
    if git("fetch", "-q", "origin", "main").returncode == 0:
        git("merge", "-q", "--ff-only", "FETCH_HEAD")


def main() -> int:
    json.load(sys.stdin)
    from shiftboss import brief, config
    from shiftboss.paths import project_root
    from shiftboss.state import read_state, write_state
    sync_main(project_root())
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        brief.main([])
    text = out.getvalue().rstrip()
    result = {"systemMessage": text}
    line = next((row for row in text.splitlines() if row.startswith(PREFIX)), "")
    items = line.removeprefix(PREFIX)
    store = config.control_dir(brief.inbox.origin_git_dir(project_root())) / "stop-block.json"
    if items and items != "：无":
        now = parse(items)
        before = (read_state(store) or {}).get("items") or {}
        write_state(store, {"items": now})
        if grew(now, before):
            result.update(decision="block", reason=f"收尾前先处理简报「该你处理」：{items}。逐项处理，或说明为何暂不处理后再收尾。")
    else:
        write_state(store, {"items": {}})
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
