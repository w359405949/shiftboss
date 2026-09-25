"""平台适配：与操作系统打交道的操作走随包分发的原生脚本（`scripts/windows/*.ps1`、`scripts/posix/*.sh`），逻辑留在 Python。

1. 参数经一个临时文件传入：UTF-8，字段以 NUL 分隔，顺序由各操作定；内容不经命令行字符串拼接。
2. 脚本在标准输出最后一行打印一行 JSON：`{"ok": true, ...}` 或 `{"ok": false, "error": "..."}`；退出码非零即失败。
3. 缺某平台脚本时报「未支持」，不回退到 Python 模拟。
4. 杀进程树与列进程用 psutil，它本身跨平台，不另写脚本。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).with_name("scripts")


def _command(operation: str) -> list[str]:
    if os.name == "nt":
        path = SCRIPTS / "windows" / f"{operation}.ps1"
        shell = shutil.which("pwsh") or shutil.which("powershell")
        command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    else:
        path = SCRIPTS / "posix" / f"{operation}.sh"
        shell = shutil.which("sh")
        command = [shell, str(path)]
    if not path.exists() or not shell:
        raise RuntimeError(f"本平台未支持「{operation}」：缺 {path} 或其解释器")
    return command


def run(operation: str, fields: list[str]) -> dict:
    """调用一个平台脚本，返回它打印的那行 JSON；失败抛 RuntimeError。"""
    if any("\0" in field for field in fields):
        raise ValueError("参数里不能含 NUL")
    handle, name = tempfile.mkstemp(prefix=f"shiftboss-{operation}-", suffix=".args")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write("\0".join(fields).encode("utf-8"))
        proc = subprocess.run([*_command(operation), name], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    finally:
        os.unlink(name)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        result = {"ok": False, "error": (proc.stderr or proc.stdout).strip() or f"退出码 {proc.returncode}"}
    if proc.returncode or not result.get("ok"):
        raise RuntimeError(f"{operation} 失败：{result.get('error') or proc.stderr.strip()}")
    return result


def launch(argv: list[str], cwd: str, title: str) -> None:
    """在新终端标签页里起一个交互会话，标签以 `title` 命名。"""
    run("launch", [cwd, title, *argv])


def spawn(argv: list[str], cwd: str) -> int:
    """后台起一个常驻进程，脱离当前终端与父进程；返回 PID。"""
    return int(run("spawn", [cwd, *argv])["pid"])
