"""定位并描述本机 Antigravity CLI（`agy`）。**agy 的会话细节只有这一份，派活与收件箱都从这里取。**

安装位置 `%LOCALAPPDATA%\\agy\\bin\\agy.exe`，不在 `PATH` 上；状态在
`~/.gemini/antigravity-cli/`：

- `cache/last_conversations.json`：工作目录 → 该目录最近一个会话号；
- `conversations/<会话号>.db`：SQLite，`steps` 是逐步记录，`executor_metadata`
  与 `gen_metadata` 里有实际使用的模型名；
- `settings.json` 的 `trustedWorkspaces`：受信任的目录。

与 Codex 的差别只有一处影响派活：**没有往活会话里投消息的命令**。
会话停在等输入时由派活方关掉再 `--conversation` 带着交代恢复；在干活时拒派。

`--conversation <id> -i <提示词>` 接上原会话并把提示词作为新一轮输入（已实测）；历史大的会话恢复后
改写到一个新会话号，所以恢复同样要按副本重取会话号。会话库是 WAL 模式，最后写入时刻看 `-wal` 文件。
`steps` 末行 `step_type` 为 15（模型回复）且 `status` 为 3（完成）即停在等输入（已对停住的会话实测，
干活中的会话未测）。启动先登录，会话库要等登录完成才建。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from shiftboss.paths import same_dir

HOME = Path.home() / ".gemini" / "antigravity-cli"
PROCESS_NAMES = ("agy.exe", "agy")
CAN_QUEUE = False
# 起来先登录，会话记录要等登录完、第一轮开始才写；恢复也可能换成新会话号。
SESSION_WAIT = 180
SKIP_PERMISSIONS = "--dangerously-skip-permissions"
REPLY_STEP, DONE_STATUS = 15, 3
MODEL_NAME = re.compile(rb"(?:gemini|claude|gpt)[\w.\-]{2,60}")


def resolve_agy() -> Path:
    localapp = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    exe = localapp / "agy/bin/agy.exe"
    if exe.exists():
        return exe
    found = shutil.which("agy")
    if found:
        return Path(found)
    raise SystemExit("找不到 agy.exe：查过 %LOCALAPPDATA%/agy/bin/agy.exe 与 PATH。")


def tui_command(copy: str, prompt: str, *, resume_id: str | None = None) -> list[str]:
    """工作目录由启动方设为副本；agy 没有 `-C`。"""
    return ([str(resolve_agy()), SKIP_PERMISSIONS]
            + (["--conversation", resume_id] if resume_id else []) + ["-i", prompt])


def configured(argv: list[str]) -> bool:
    return SKIP_PERMISSIONS in argv



def database(session_id: str | None) -> Path | None:
    path = HOME / "conversations" / f"{session_id}.db" if session_id else None
    return path if path and path.exists() else None


def last_write(session_id: str | None) -> float | None:
    path = database(session_id)
    if path is None:
        return None
    wal = path.with_name(path.name + "-wal")
    return max(path.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0.0)


def step_count(session_id: str | None) -> int:
    rows = _query(session_id, "select count(*) from steps")
    return rows[0][0] if rows else 0


def newest_session(copy_win: str, since: float, previous: str | None = None,
                   previous_steps: int | None = None) -> str | None:
    """`since` 之后这个副本上在用的会话号。

    新会话按会话库创建时刻判。恢复时仍是 `previous` 的，要它的步数比 `previous_steps` 多才算：
    恢复换号时旧库也会被打开，WAL 修改时刻因此变新，不能当作「还在用旧号」的证据。
    """
    try:
        mapping = json.loads((HOME / "cache/last_conversations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for directory, session_id in mapping.items():
        path = database(session_id) if same_dir(directory, copy_win) else None
        if path is None:
            continue
        if session_id == previous:
            grown = previous_steps is not None and step_count(session_id) > previous_steps
            return session_id if grown else None
        if path.stat().st_ctime >= since - 2:
            return session_id
    return None


def _query(session_id: str | None, sql: str) -> list[tuple]:
    path = database(session_id)
    if path is None:
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def parked_at(session_id: str | None) -> str | None:
    """会话停在等输入上的时刻（会话库修改时刻）；在干活或读不到返回 None。"""
    rows = _query(session_id, "select step_type, status from steps order by idx desc limit 1")
    if not rows or rows[0] != (REPLY_STEP, DONE_STATUS):
        return None
    stamp = last_write(session_id)
    return datetime.datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")


def model_of(session_id: str | None) -> str | None:
    """实际使用的模型；带档位的写法（如 `gemini-3.8-flash-high`）优先。"""
    names = []
    for table in ("executor_metadata", "gen_metadata"):
        for (blob,) in _query(session_id, f"select data from {table} order by idx desc limit 1"):
            names += [m.decode("ascii") for m in MODEL_NAME.findall(blob or b"")]
    return max(names, key=len) if names else None


def trust(copies: list[Path], check: bool) -> bool:
    """把副本登记进 `trustedWorkspaces`；`check` 时只报告缺项。"""
    path = HOME / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"agy 配置读不动：{path}（{exc}）") from exc
    trusted = settings.setdefault("trustedWorkspaces", [])
    missing = [str(p) for p in copies if not any(same_dir(str(p), t) for t in trusted)]
    if not missing:
        return True
    if check:
        print("agy 未信任的副本：" + "、".join(missing))
        return False
    trusted.extend(missing)
    from shiftboss.state import write_state
    write_state(path, settings)
    print("agy 信任登记：" + "、".join(missing))
    return True


def prepare(copies: list[Path], check: bool) -> bool:
    """没有逐次传入信任的办法，副本登记进 `trustedWorkspaces`。"""
    return trust(copies, check)


def queue_into(session_id: str, message: str, alive) -> bool:
    """没有往活会话里投消息的命令。"""
    return False


def sandbox_warning(session_id: str | None) -> str | None:
    """权限每次启动时经命令行给定（见 `configured`），会话里不另记。"""
    return None
