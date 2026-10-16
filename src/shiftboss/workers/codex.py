"""Codex 执行方插件：定位 CLI、起 TUI、读 rollout、往活会话投消息。**Codex 的会话细节只有这一份。**

非 Windows（含 WSL）下 `codex` 在 `PATH` 上，直接用。Windows 下 `codex.exe` 不在 `PATH` 上，安装路径里夹着一段随版本变的哈希：

    %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe

前缀是稳定的，所以只解析最后那一段——在那一个目录里列一层，按修改时间取最新的。
不扫全盘：全盘搜索要几秒，这里是一次目录列举。找不到再依次退回 VS Code 扩展
自带的那份和 `PATH`。三处都没有就报错退 127，不猜。

用法：

    python -B -m shiftboss.workers.codex --resolve-only
    python -B -m shiftboss.workers.codex exec -C <副本> "提示词"

其余参数原样透传，退出码一并传回。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from shiftboss.paths import same_dir

MISSING = """找不到 codex.exe。依次查过：
  %LOCALAPPDATA%/OpenAI/Codex/bin/<hash>/codex.exe
  %USERPROFILE%/.vscode/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe
  PATH
装了却没找到，说明安装布局变了，改本文件的 resolve_codex，不要在调用处写死路径。"""


def resolve_codex() -> Path:
    """找到 `codex.exe`；非 Windows（含 WSL）只认 `PATH` 上的 `codex`。

    **调用方不要经过 bash 去找它**——`bash` 在 `PATH` 上可能是 WSL 的那个，
    WSL 坏掉时它会失败，而 `Popen` 依然成功返回，于是「已派出」是假的：
    派活台账记下了会话，实际什么都没起来。
    """
    if os.name != "nt":
        found = shutil.which("codex")
        if found:
            return Path(found)
        raise SystemExit("找不到 codex：非 Windows 只查 PATH。")
    localapp = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    hits = sorted((localapp / "OpenAI/Codex/bin").glob("*/codex.exe"),
                  key=lambda q: q.stat().st_mtime, reverse=True)
    if hits:
        return hits[0]
    ext = sorted(Path.home().glob(
        ".vscode/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
    if ext:
        return ext[-1]
    found = shutil.which("codex")
    if found:
        return Path(found)
    raise SystemExit(MISSING)


PROCESS_NAMES = ("codex.exe", "codex")
CAN_QUEUE = True
SESSION_WAIT = 30
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def configured(argv: list[str]) -> bool:
    """按派活方式起的：`-C` 指副本、权限全开、逐次传入副本信任。"""
    return (any(arg in ("-C", "--cd") for arg in argv)
            and "danger-full-access" in argv and "never" in argv
            and any("trust_level" in arg and "trusted" in arg for arg in argv))


def step_count(session_id: str | None) -> int | None:
    """Codex 的恢复靠 `queue`，不需要按步数判接上与否。"""
    return None


def tui_command(copy_win: str, prompt: str, *, resume_id: str | None = None) -> list[str]:
    """新派、续开、恢复共用：信任只传给这一次 TUI，不读写共享配置。

    新派、续开和恢复 TUI 都用 `-c` 逐次传入副本信任；准备和派活代码不读写
      `~/.codex/config.toml`，也没有写共享配置的兜底路径。
    """
    # 路径是 TOML 表的带引号键，整个表是一个配置值，避免路径里的点被当成层级。
    project = json.dumps(str(Path(copy_win).resolve()), ensure_ascii=False)
    trust = "projects={" + project + '={trust_level="trusted"}}'
    return ([str(resolve_codex()), "-C", copy_win, "-c", trust, "--search",
             "-s", "danger-full-access", "-a", "never"]
            + (["resume", resume_id] if resume_id else []) + [prompt])


def rollout_of(session_id: str) -> Path | None:
    hits = list((CODEX_HOME / "sessions").rglob(f"rollout-*{session_id}.jsonl"))
    return hits[0] if hits else None


def newest_session(copy_win: str, since: float, previous: str | None = None,
                   previous_steps: int | None = None) -> str | None:
    """`since` 之后在这个副本里**新建**的 TUI 会话号。派完立刻记下来，好让下一轮投得进去。

    **判据是会话的创建时刻，不是 rollout 的 mtime。** 同一个副本上如果还有一个
    仍在干活的旧会话，它的 rollout 一直在被写，mtime 永远是新的——按 mtime 找
    就会把那个旧会话当成刚起的这个记进台账。2026-09-09 重启分析线时就这么错过一次：
    新任务书的台账里记下的是上一条线的会话号。
    """
    newest = None
    for path in (CODEX_HOME / "sessions").rglob("rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < since - 2:
                continue
            meta = json.loads(path.open(encoding="utf-8").readline()).get("payload", {})
        except (OSError, ValueError):
            continue
        if not ((str(meta.get("originator", "")).startswith("codex-tui")
                 or meta.get("originator") == "codex_vscode")
                and same_dir(meta.get("cwd"), copy_win)):
            continue
        created = session_started(meta)
        if created is None or created < since - 2:
            continue  # 旧会话被写热了而已，不是这一次起的
        if newest is None or created > newest[0]:
            newest = (created, str(meta.get("session_id", "")) or None)
    return newest[1] if newest else None


def session_started(meta: dict) -> float | None:
    """rollout 头一行记的会话创建时刻（epoch 秒）；读不出来就返回 None。"""
    stamp = meta.get("timestamp")
    if not isinstance(stamp, str):
        return None
    import datetime
    try:
        text = stamp.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def model_of(session_id: str | None) -> str | None:
    """实际使用的模型，带推理档位；读不到返回 None。"""
    roll = rollout_of(session_id) if session_id else None
    model = None
    try:
        for line in (roll.read_text(encoding="utf-8", errors="replace").splitlines() if roll else []):
            if '"turn_context"' not in line:
                continue
            payload = json.loads(line).get("payload", {})
            effort = (payload.get("collaboration_mode") or {}).get("settings", {}).get("reasoning_effort")
            model = payload.get("model") and "/".join(filter(None, (payload["model"], effort)))
    except (OSError, ValueError):
        return None
    return model


def parked_at(session_id: str | None) -> str | None:
    """rollout 末事件是 `task_complete` 即停在等输入，返回其时刻；在干活或读不到返回 None。"""
    roll = rollout_of(session_id) if session_id else None
    if roll is None:
        return None
    last_type, last_ts = "", None
    try:
        for line in roll.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            kind = (row.get("payload") or {}).get("type") or ""
            if kind:
                last_type, last_ts = kind, row.get("timestamp")
    except (OSError, ValueError):
        return None
    return last_ts if last_type == "task_complete" else None


def sandbox_warning(sid: str | None) -> str | None:
    """最后一轮 `turn_context` 记的沙箱不是全开时返回告警。"""
    roll = rollout_of(sid) if sid else None
    sandbox = None
    try:
        if roll:
            with roll.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # A concurrently appended last line may be incomplete.
                    if record.get("type") == "turn_context":
                        sandbox = record.get("payload", {}).get("sandbox_policy", "未确认")
    except OSError as exc:
        sandbox = f"未确认：{exc}"
    mode = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
    if mode == "danger-full-access":
        return None
    return f"最后一轮沙箱 {sandbox or '未确认（没有 turn_context）'}"


def texts_of(content) -> str:
    """把一条消息的 content 摊平成纯文本。

    **不要拿 `json.dumps` 的结果去比对**：投进去的消息里有真换行，
    而 rollout 与 queue 存的是转义后的 `\n`，字面比对必然落空。
    2026-09-02 我因此第二次误报「没投进去」——第一次是会话忙，这次是换行。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return "".join(texts_of(v) for k, v in content.items()
                       if k in ("text", "content", "input_text"))
    if isinstance(content, list):
        return "".join(texts_of(x) for x in content)
    return ""


def parked_in_queue(session_id: str, message: str) -> bool:
    """这条消息还躺在 `~/.codex/queue_1.sqlite` 里等这个会话消费。"""
    db = Path(os.path.expanduser("~/.codex/queue_1.sqlite"))
    if not db.exists():
        return False
    head = message[:40]
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select payload_json from queued_items where thread_id = ?",
                           (session_id,)).fetchall()
        con.close()
    except sqlite3.Error:
        return False
    for (payload,) in rows:
        try:
            body = json.loads(payload or "{}")
        except ValueError:
            continue
        if head in texts_of(body.get("UserInput", {}).get("content")):
            return True
    return False


def queue_into(session_id: str, message: str, alive=None) -> bool:
    """往活着的会话里投一条（只供 `--recover` 接回意外中断的本轮），**返回是否真的投进去了**。

    **`codex queue` 的退出码不是判据**：会话已经死了它照样退 0、照样打印
    「Queued message」，那条消息进 `~/.codex/queue_1.sqlite` 躺着，永远不执行。
    2026-09-01 实测。

    **但「rollout 没长出来」也不等于没投进去。** 会话正忙时 `queue` 就是先把消息
    停在那张表里，等它空闲再消费——2026-09-02 我据此误报了一次「没投进去」，
    而调用方看见 False 会去 `resume`，**在同一个副本上开出第二个会话**。
    所以判据分三档：rollout 里真的多了一条 → 已消费；还停在队列里、且这个副本上
    TUI 还活着 → **也算投进去了**，只是等它忙完；两样都不是 → 才是丢了。
    `alive`（判这个副本上 TUI 还活着的函数）空着时退化成只认第一档（宁可误报丢了，也不要凭空当成活的）。
    """
    roll = rollout_of(session_id)
    if roll is None:
        return False

    def hits() -> int:
        """rollout 里有几条**用户消息**装着这段文本。

        判据是这个数**变多**，不是「有没有」，也不是「rollout 长没长大」：

        - 「rollout 长大了」对**在干活的会话永远为真**（它自己一直在写事件）。
          2026-09-01 我就是这么连着两次上报「投递成功」，而消息其实一直躺在
          `~/.codex/queue_1.sqlite` 里没被消费——**忙碌的会话收不下 queue**。
        - 「有没有这段文本」也不行：投进去的是**固定不变的提示词**，
          上一轮那条一直在 rollout 里，一查就命中，同样永远为真。
        """
        head = message[:40]
        n = 0
        for line in roll.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                pl = json.loads(line).get("payload", {})
            except ValueError:
                continue
            if (pl.get("type") == "message" and pl.get("role") == "user"
                    and head in texts_of(pl.get("content"))):
                n += 1
        return n

    before = hits()
    # **`queue` 要给超时。** 会话已经关掉时它会卡住不返回，而这一刻任务书的交代
    # 已经提交推送了——外面看见的是「派活命令没反应」，台账却还没更新，
    # 掐掉重来就会把交代写第二遍。卡住等同于投不进去，交给下面 resume 那一档。
    try:
        subprocess.run([str(resolve_codex()), "queue", "--thread", session_id,
                        "--message", message], capture_output=True, text=True,
                       timeout=30)
    except subprocess.TimeoutExpired:
        return False
    for _ in range(30):
        time.sleep(1)
        if hits() > before:
            return True
    if alive and parked_in_queue(session_id, message) and alive():
        print("  （会话正忙，消息停在 codex 队列里等它空闲——这也算投进去了）")
        return True
    return False


def prepare(copies: list[Path], check: bool) -> bool:
    """信任随每次启动经 `-c` 传入（见 `tui_command`），副本不用登记。"""
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        exe = resolve_codex()
    except SystemExit as err:
        print(err, file=sys.stderr)
        return 127
    if args[:1] == ["--resolve-only"]:
        print(exe)
        return 0
    return subprocess.run([str(exe), *args]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
