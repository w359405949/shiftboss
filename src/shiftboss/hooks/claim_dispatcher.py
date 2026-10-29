"""UserPromptSubmit 钩子：用户发话的会话认领派活方，规则见 `shiftboss.state.require_dispatcher`。

会话号取钩子输入的 `session_id`，没有输入时取宿主的 Claude Code 或 Codex 会话身份。
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        session_id = json.load(sys.stdin).get("session_id")
    except (json.JSONDecodeError, ValueError, AttributeError):
        session_id = None
    from shiftboss.state import claim_dispatcher, current_session_id
    session_id = session_id or current_session_id()
    if not session_id:
        return 0
    claim_dispatcher(session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
