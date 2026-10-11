"""派活方的每次汇报末尾须原样附上 `shiftboss brief` 的输出（含 `【在办】`）。

用户要求每次总结都能直接看到在办任务与门铃状态，用来与后台实际情况对照；靠记忆附会漏，所以由钩子拦。
这一轮最后的回复里没有就不让结束，并说明怎么补。已因本钩子续过一次的（`stop_hook_active`）放行，免得来回打转。
钩子自身出错时放行。
"""
from __future__ import annotations

import json
import sys

from shiftboss.brief import MARKER


def last_reply(transcript: str) -> str:
    """最后一条用户输入之后，派活方写出的全部文字。"""
    texts: list[str] = []
    with open(transcript, encoding="utf-8") as handle:
        for raw in handle:
            row = json.loads(raw)
            message = row.get("message") or {}
            content = message.get("content")
            if row.get("type") == "user":
                is_prompt = isinstance(content, str) or any(
                    isinstance(part, dict) and part.get("type") == "text" for part in content or [])
                if is_prompt:
                    texts = []
            elif row.get("type") == "assistant":
                for part in content or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(part.get("text", ""))
    return "\n".join(texts)


def main() -> int:
    payload = json.load(sys.stdin)
    if payload.get("stop_hook_active"):
        return 0
    transcript = payload.get("transcript_path")
    if not transcript or MARKER in last_reply(transcript):
        return 0
    print(json.dumps({"decision": "block", "reason": (
        "汇报末尾缺状态摘要：跑 `cd /home/w/MMEditor/MMEditor && shiftboss brief`，"
        "把输出原样附在回复末尾（代码块），再结束。")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
