"""甲方生命周期事件自动接回意外退出的任务，不重启被用户中断的主会话。

SessionStart(startup/resume/compact/clear)、Interrupt 与 UserPromptSubmit 共用入口。
Interrupt 只检查其他台账任务；原会话的用户取消仍由 session_state 判为停下。
钩子只启动短小后台作业，恢复过程、退出码和错误留在 control/auto-recover*。
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

EVENTS = frozenset(("SessionStart", "Interrupt", "UserPromptSubmit"))
MODULE = "shiftboss.hooks.auto_recover"
RETRY_SECONDS = 60


def authorized(repo: Path, payload: dict) -> bool:
    from shiftboss.config import control_dir
    from shiftboss.inbox import origin_git_dir
    from shiftboss.state import read_state
    cwd, session = payload.get("cwd"), payload.get("session_id")
    if (payload.get("hook_event_name") not in EVENTS or not isinstance(cwd, str)
            or Path(cwd).resolve() not in (repo, repo.parent)
            or not isinstance(session, str) or not session):
        return False
    owner = read_state(control_dir(origin_git_dir(repo), repo) / "dispatcher.json").get("session")
    return session == owner


def launch(repo: Path, payload: dict) -> int:
    from shiftboss import config, native
    if not authorized(repo, payload) or not config.automation("session_recover", repo):
        return 0
    argv = [sys.executable, "-B", "-m", MODULE, "--repo", str(repo), "--run",
            "--session", payload["session_id"], "--event", payload["hook_event_name"]]
    # 不继承 hook stdin，不等待 resume CLI，更不向当前主会话投「继续」。
    native.spawn(argv, str(repo))
    return 0


def perform(repo: Path, payload: dict) -> int:
    from shiftboss import config, recover
    from shiftboss.inbox import origin_git_dir
    from shiftboss.state import read_state, write_state
    if not authorized(repo, payload) or not config.automation("session_recover", repo):
        return 0
    directory = config.control_dir(origin_git_dir(repo), repo)
    with recover.recovery_lock(repo) as acquired:
        if not acquired:
            return 0
        target = directory / "auto-recover.json"
        previous = read_state(target)
        # 顺序到达的重放事件和启动失败同样限频；下次事件到达才重试，不自旋。
        if time.time() - previous.get("started_epoch", 0) < RETRY_SECONDS:
            return 0
        entry = {"event": payload["hook_event_name"], "session": payload["session_id"],
                 "started_epoch": time.time(), "pid": os.getpid(), "status": "running"}
        write_state(target, entry)
        log_path = directory / "auto-recover.log"
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.replace(log_path.with_suffix(".previous.log"))
        code = 1
        with log_path.open("a", encoding="utf-8", buffering=1) as log:
            with redirect_stdout(log), redirect_stderr(log):
                print(f"\n{datetime.now(timezone.utc).isoformat()} {entry['event']} pid={entry['pid']}")
                try:
                    # 子 CLI 输出也进日志；错误必须保留实际返回码。
                    recover.REPO = repo
                    code = recover.run(False, output=log)
                except Exception as exc:
                    print(f"自动恢复启动失败：{type(exc).__name__}: {exc}")
                finally:
                    entry.update(status="completed" if code == 0 else "failed", returncode=code,
                                 completed_at=datetime.now(timezone.utc).isoformat())
                    write_state(target, entry)
        return code


def install(repo: Path, path: Path) -> bool:
    """幂等安装到明确指定的工作区配置；保留所有无关事件和处理器。"""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks 必须是对象")
    argv = [sys.executable, "-B", "-m", MODULE, "--repo", str(repo)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    before = json.dumps(data, sort_keys=True)
    for event in sorted(EVENTS):
        groups = hooks.setdefault(event, [])
        found = False
        for group in groups:
            for handler in group.get("hooks", []):
                old = handler.get("command", "")
                if MODULE in old:
                    handler.update(type="command", command=command, timeout=3 if event == "Interrupt" else 15)
                    found = True
        if not found:
            groups.append({"hooks": [{"type": "command", "command": command,
                                       "timeout": 3 if event == "Interrupt" else 15}]})
    if json.dumps(data, sort_keys=True) == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--event", choices=sorted(EVENTS))
    parser.add_argument("--install-hooks", type=Path, help="幂等安装到明确指定的 Codex hooks.json；不改信任配置")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.install_hooks:
        print("已安装恢复事件钩子。" if install(repo, args.install_hooks) else "恢复事件钩子已是当前配置。")
        return 0
    if args.run:
        if not args.session or not args.event:
            parser.error("--run 需要 --session 与 --event")
        os.environ["CODEX_THREAD_ID"] = args.session
        return perform(repo, {"cwd": str(repo), "session_id": args.session, "hook_event_name": args.event})
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
    except (ValueError, OSError):
        return 0
    if payload.get("hook_event_name") == "UserPromptSubmit":
        # 钩子之间可能并行；把既有认领放在本入口内先完成，不依赖处理器顺序。
        result = subprocess.run([sys.executable, "-B", "-m", "shiftboss.hooks.claim_dispatcher",
                                 "--repo", str(repo)], input=json.dumps(payload), text=True, timeout=10)
        if result.returncode:
            return result.returncode
    return launch(repo, payload)


if __name__ == "__main__":
    raise SystemExit(main())
