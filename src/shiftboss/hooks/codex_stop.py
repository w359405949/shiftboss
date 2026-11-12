"""Render the registered dispatcher's complete Stop brief exactly once per turn.

The project-local launcher delegates here. Hook failures are visible and retain
their complete output; they never masquerade as an empty successful brief.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


def record(directory: Path, payload: dict, stage: str, **details) -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(), "stage": stage,
             **{key: payload.get(key) for key in ("session_id", "turn_id", "cwd", "hook_event_name")},
             **details}
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "shiftboss-stop-runs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def deliver(directory: Path, payload: dict, response: dict) -> dict:
    response = dict(response)
    text = response["systemMessage"]
    snapshot = directory / "shiftboss-stop-brief.json"
    previous = json.loads(snapshot.read_text(encoding="utf-8")) if snapshot.exists() else {}
    turn_id = payload.get("turn_id")
    already_displayed = (bool(turn_id)
                         and previous.get("session_id") == payload.get("session_id")
                         and previous.get("turn_id") == turn_id
                         and previous.get("brief_displayed", False))
    display = not already_displayed and response.get("decision") != "block"
    if not display:
        response.pop("systemMessage", None)
    entry = {"session_id": payload.get("session_id"), "turn_id": turn_id,
             "at": datetime.now(timezone.utc).isoformat(), "brief": text,
             "brief_displayed": already_displayed or display}
    temporary = snapshot.with_suffix(".tmp")
    temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(snapshot)
    record(directory, payload, "delivery", displayed=display,
           stop_hook_active=bool(payload.get("stop_hook_active")))
    return response


def failure(directory: Path, payload: dict, reason: str, **details) -> dict:
    error = {"at": datetime.now(timezone.utc).isoformat(), "reason": reason, **details}
    path = directory / "shiftboss-stop-last-error.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(error, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record(directory, payload, "failed", reason=reason, error_file=str(path))
        location = f"完整错误：{path}"
    except OSError as exc:
        location = f"错误记录也未能写入：{exc}"
    return {"systemMessage": f"ShiftBoss 简报失败：{reason}。未取得完整简报，不能据此认定无待办。{location}"}


def output_text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--session-id")  # Retain compatibility with the local launcher.
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    directory = args.state_dir or repo.parent / ".codex"
    payload = {}
    command_output = {}
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        record(directory, payload, "received")
        from shiftboss.config import control_dir
        from shiftboss.inbox import origin_git_dir
        from shiftboss.state import read_state
        owner = (read_state(control_dir(origin_git_dir(repo), repo) / "dispatcher.json") or {}).get("session")
        cwd = payload.get("cwd")
        if (payload.get("hook_event_name") != "Stop" or not owner
                or payload.get("session_id") != owner
                or not cwd or Path(cwd).resolve() not in (repo, repo.parent)):
            record(directory, payload, "skipped")
            print("{}")
            return 0
        result = subprocess.run(
            [sys.executable, "-B", "-m", "shiftboss.hooks.show_brief"],
            input=json.dumps(payload), cwd=repo, capture_output=True, text=True, timeout=150,
        )
        command_output = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        if result.returncode:
            response = failure(directory, payload, f"简报命令退出 {result.returncode}",
                               returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        else:
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or not isinstance(response.get("systemMessage"), str) or not response["systemMessage"]:
                raise ValueError("简报命令没有返回完整 systemMessage")
            response = deliver(directory, payload, response)
            record(directory, payload, "completed", has_brief=bool(response.get("systemMessage")),
                   decision=response.get("decision"))
    except subprocess.TimeoutExpired as exc:
        response = failure(directory, payload, f"简报生成超过 {exc.timeout} 秒",
                           timeout=exc.timeout, stdout=output_text(exc.stdout), stderr=output_text(exc.stderr))
    except Exception as exc:
        response = failure(directory, payload if isinstance(payload, dict) else {},
                           f"{type(exc).__name__}: {exc}", **command_output)
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
