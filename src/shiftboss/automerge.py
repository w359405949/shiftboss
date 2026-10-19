"""自动合入：满足条件的交回由常驻巡检在独立副本里合并、推送、归档，派活方事后复核。

适用的子类型由项目在 `shiftboss.toml` 的 `[automerge] subtypes` 登记（不登记即不启用）。条件：
交回执行状态「完成」、无待议、无「待发布后补」、非性能类、破坏性为「否」；合并本身仍走 `shiftboss merge`
（快速检查、写入范围、合入前置照常拦）。拒合的记下原因、同一提交不再重试，留给派活方。
自动合入的线在状态摘要「该你处理」列为「复核自动合入」，派活方读完交回后 `--ack`。

用法：
    shiftboss automerge --list          # 已自动合入、待复核的线
    shiftboss automerge --ack <任务书名>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from shiftboss import config
from shiftboss.state import read_state, write_state


def store(remote: Path) -> Path:
    return config.control_dir(remote) / "automerge.json"


def subtypes(workspace: Path) -> list[str]:
    path = workspace / config.CONFIG_NAME
    if not path.exists():
        return []
    with path.open("rb") as handle:
        return list(tomllib.load(handle).get("automerge", {}).get("subtypes", []))


def git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=check)


PUSH_ATTEMPTS = 3


def push(workspace: Path) -> None:
    """main 在合并期间前进（派活方或常驻验证推送）时先并入再推；main 推得频繁时并入后可能又被抢先，重试几次。"""
    for _ in range(PUSH_ATTEMPTS - 1):
        if git(workspace, "push", "--quiet", "origin", "HEAD:main", check=False).returncode == 0:
            return
        git(workspace, "pull", "--quiet", "--no-rebase", "--no-edit", "origin", "main")
    git(workspace, "push", "--quiet", "origin", "HEAD:main")


def candidates(workspace: Path, allowed: list[str]):
    """→ (任务书名, 分支顶端提交)。条件见模块说明。"""
    from shiftboss.merge import pending_lines, publish_later_lines
    from shiftboss.taskbook import Taskbook, category_of, execution_status
    for book in Taskbook.iter_active(workspace):
        meta = book.meta()
        if meta.get("子类型") not in allowed or category_of(meta) == "性能" or meta.get("破坏性") != "否":
            continue
        ref = f"origin/{meta.get('分支') or 'codex/' + book.name}"
        tip = git(workspace, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
        if not tip or git(workspace, "merge-base", "--is-ancestor", tip, "origin/main", check=False).returncode == 0:
            continue
        delivery = git(workspace, "show", f"{tip}:{book.path.relative_to(workspace)}/delivery.md", check=False).stdout
        if execution_status(delivery) != "完成" or pending_lines(delivery) or publish_later_lines(delivery):
            continue
        yield book.name, tip


def run(remote: Path, workspace: Path, ring) -> None:
    """巡检每轮调用一次。独立副本只用于合并与推送，每次从 origin/main 重置。"""
    git(workspace, "fetch", "--quiet", "origin")
    git(workspace, "checkout", "--quiet", "-B", "main", "origin/main")
    git(workspace, "reset", "--quiet", "--hard", "origin/main")
    git(workspace, "clean", "-fdq", "-e", "out/")
    allowed = subtypes(workspace)
    if not allowed:
        return
    state = read_state(store(remote)) or {"merged": {}, "refused": {}}
    for name, tip in list(candidates(workspace, allowed)):
        if state["refused"].get(name, {}).get("tip") == tip:
            continue
        py = [sys.executable, "-B", "-m", "shiftboss.merge"]
        merged = subprocess.run([*py, tip], cwd=workspace, capture_output=True, text=True)
        if merged.returncode:
            git(workspace, "merge", "--abort", check=False)
            git(workspace, "reset", "--quiet", "--hard", "origin/main")
            record(remote, "refused", name, {"tip": tip, "at": time.time(), "reason": (merged.stdout + merged.stderr)[-600:]})
            ring(remote, f"automerge refused {name} @ {tip[:12]}; run shiftboss automerge --list")
            continue
        push(workspace)
        subprocess.run([*py, "--done", name], cwd=workspace, capture_output=True, text=True)
        push(workspace)
        record(remote, "merged", name, {"tip": tip, "at": time.time(), "acked": False})
        record(remote, "refused", name, None)
        ring(remote, f"automerged {name} @ {tip[:12]}; review its delivery, then shiftboss automerge --ack {name}")


def record(remote: Path, section: str, name: str, value: dict | None) -> None:
    """只改一条记录：写回前重读整份状态，不覆盖运行期间别的进程（如 `--ack`）写入的记录。"""
    state = read_state(store(remote)) or {"merged": {}, "refused": {}}
    bucket = state.setdefault(section, {})
    if value is None:
        bucket.pop(name, None)
    else:
        bucket[name] = value
    write_state(store(remote), state)


def unreviewed(remote: Path) -> list[str]:
    state = read_state(store(remote)) or {}
    return sorted(name for name, item in (state.get("merged") or {}).items() if not item.get("acked"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shiftboss automerge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--ack", metavar="任务书名")
    args = parser.parse_args(argv)
    from shiftboss.paths import project_root
    from shiftboss.verify import origin
    remote = origin(project_root())
    state = read_state(store(remote)) or {"merged": {}, "refused": {}}
    if args.ack:
        if args.ack not in state.get("merged", {}):
            raise SystemExit(f"{args.ack} 不在自动合入记录里")
        state["merged"][args.ack]["acked"] = True
        write_state(store(remote), state)
        print(f"已复核 {args.ack}")
        return 0
    for name in unreviewed(remote):
        print(f"待复核 {name} @ {state['merged'][name]['tip'][:12]}")
    for name, item in (state.get("refused") or {}).items():
        print(f"拒合 {name} @ {item['tip'][:12]}：{item['reason'].strip().splitlines()[-1] if item['reason'].strip() else ''}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
