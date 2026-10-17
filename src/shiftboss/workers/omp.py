"""定位并描述本机 oh-my-pi CLI（`omp`）。**omp 的会话细节只有这一份，派活与收件箱都从这里取。**

状态在 `PI_CODING_AGENT_DIR`（缺省 `~/.omp/agent`）：

- `sessions/<目录编码>/<时刻>_<会话号>.jsonl`：一行一条记录；目录编码有损，副本以 `session` 行的 `cwd` 为准；
- `type` 为 `message` 的行是逐条消息，`model_change`、`thinking_level_change` 记模型与档位。

与 agy 相同，**没有往活会话里投消息的命令**：会话停在等输入时由派活方关掉再 `--resume` 带着交代恢复；在干活时拒派。

1. `--resume <会话号> <提示词>` 沿用原会话号，追加进同一个文件（已实测）。
2. 末条消息是 `assistant` 且 `stopReason` 不是 `toolUse` 即停在等输入。
3. 一个 TUI 一个 `omp.exe` 进程，工作目录即启动目录（已实测）。
4. 审批模式每次启动传入，不依赖共享配置；模型由用户在 `config.yml` 的 `modelRoles` 指定。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from shiftboss.paths import same_dir

HOME = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".omp" / "agent")
PROCESS_NAMES = ("omp.exe", "omp")
CAN_QUEUE = False
# 起来先登录，会话记录要等登录完、第一轮开始才写；恢复也可能换成新会话号。
SESSION_WAIT = 180
APPROVAL = ("--approval-mode", "yolo")
# omp 的中文写作不可靠：交回、提交说明一律用英文（README 2.2 第 10 条允许）；
# 随每次启动与恢复经系统提示传入，不靠每轮交代提醒。
ENGLISH = ("You MUST write delivery.md and every commit message in English. "
           "Keep the machine-read markers exact: the first status line is `Status: progress`, "
           "`Status: done` or `Status: stopped`; mark undecided items `PENDING`; in contract.md only tick the status boxes. "
           "Reading the Chinese workflow and project documents is still required.")
# 思考档位由派活方按实际表现调（用户授权）：high 时出过思考死循环，上下文又小、一个会话压缩四五次。
THINKING = ("--thinking", "medium")


def scratch_dir(copy: str) -> Path:
    """每个副本独占的临时目录。omp 不区分会话，直接写 `/tmp/slot-before.json` 这类通用名，
    两个实例并行时互相覆盖对方的改前快照；启动时经 TMPDIR 与系统提示把它圈进这里。"""
    return Path("/tmp") / f"omp-{Path(copy).name}"
TOOL_USE = "toolUse"


def resolve_omp() -> Path:
    localapp = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    exe = localapp / "omp/omp.exe"
    if exe.exists():
        return exe
    found = shutil.which("omp")
    if found:
        return Path(found)
    raise SystemExit("找不到 omp.exe：查过 %LOCALAPPDATA%/omp/omp.exe 与 PATH。")


def tui_command(copy: str, prompt: str, *, resume_id: str | None = None) -> list[str]:
    """工作目录显式传 `--cwd`，不依赖启动方的当前目录；临时文件圈进 `scratch_dir`。"""
    scratch = scratch_dir(copy)
    scratch.mkdir(parents=True, exist_ok=True)
    system = (ENGLISH + f" Your working copy is {copy}; run every command there. "
              f"Put every scratch or temporary file under {scratch} (also $TMPDIR); never write directly under /tmp, "
              "another agent shares that directory.")
    return (["env", f"TMPDIR={scratch}", str(resolve_omp()), *APPROVAL, "--cwd", copy, *THINKING,
             "--append-system-prompt", system]
            + (["--resume", resume_id] if resume_id else []) + [prompt])


def configured(argv: list[str]) -> bool:
    return any(argv[i:i + 2] == list(APPROVAL) for i in range(len(argv) - 1))



def session_file(session_id: str | None) -> Path | None:
    hits = list((HOME / "sessions").glob(f"*/*_{session_id}.jsonl")) if session_id else []
    return hits[0] if hits else None


def _records(path: Path | None) -> list[dict]:
    """读不动或末行写到一半的跳过。"""
    rows = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path else []
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _messages(session_id: str | None) -> list[dict]:
    return [r for r in _records(session_file(session_id)) if r.get("type") == "message"]


def step_count(session_id: str | None) -> int:
    return len(_messages(session_id))


def newest_session(copy_win: str, since: float, previous: str | None = None,
                   previous_steps: int | None = None) -> str | None:
    """`since` 之后这个副本上在用的会话号。

    新会话按 `session` 行记的创建时刻判；恢复沿用 `previous`，要它的消息数比 `previous_steps` 多才算接上。
    """
    if previous:
        grown = previous_steps is not None and step_count(previous) > previous_steps
        return previous if grown else None
    import datetime
    newest = None
    for path in (HOME / "sessions").glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < since - 2:
                continue
        except OSError:
            continue
        head = next((r for r in _records(path) if r.get("type") == "session"), None)
        if not head or not same_dir(str(head.get("cwd") or ""), copy_win):
            continue
        try:
            created = datetime.datetime.fromisoformat(
                str(head.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if created >= since - 2 and (newest is None or created > newest[0]):
            newest = (created, head.get("id"))
    return newest[1] if newest else None


def parked_at(session_id: str | None) -> str | None:
    """会话停在等输入上的时刻（末条消息的时间戳）；在干活或读不到返回 None。"""
    messages = _messages(session_id)
    last = messages[-1] if messages else {}
    body = last.get("message") or {}
    if body.get("role") != "assistant" or body.get("stopReason") == TOOL_USE:
        return None
    return last.get("timestamp")


def model_of(session_id: str | None) -> str | None:
    """实际使用的模型与思考档位，如 `deepseek/deepseek-v4-flash/high`。"""
    model = level = None
    for row in _records(session_file(session_id)):
        body = row.get("message") or {}
        if row.get("type") == "model_change":
            model = row.get("model") or model
        elif row.get("type") == "thinking_level_change":
            level = row.get("thinkingLevel") or level
        elif body.get("role") == "assistant" and body.get("model"):
            model = "/".join(filter(None, (body.get("provider"), body["model"])))
    return "/".join(filter(None, (model, level))) if model else None


def prepare(copies: list[Path], check: bool) -> bool:
    """审批模式随每次启动传入，副本不用登记。"""
    return True


def queue_into(session_id: str, message: str, alive) -> bool:
    """没有往活会话里投消息的命令。"""
    return False


def sandbox_warning(session_id: str | None) -> str | None:
    """权限每次启动时经命令行给定（见 `configured`），会话里不另记；这里报末轮的接口错误。

    末条 `stopReason` 为 `error` 时 `parked_at` 照样判为停在等输入，收件箱会显示成「做完了没推」；
    实为模型服务拒收（如未开视觉编码器却收到图片），历史里留着出错的内容，`--recover` 会再撞一次，改用 `--fresh`。
    """
    messages = _messages(session_id)
    body = (messages[-1].get("message") or {}) if messages else {}
    if body.get("role") == "assistant" and body.get("stopReason") == "error":
        first = str(body.get("errorMessage") or "").splitlines()[:1]
        return f"末轮接口错误 {body.get('errorStatus') or ''}：{first[0][:120] if first else '无错误文本'}；用 --fresh 重派"
    return None
