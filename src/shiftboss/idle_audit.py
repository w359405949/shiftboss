"""空闲时触发全项目审查待办：连续两次门铃无事可做才敲，`shiftboss audit new` 按标准合同建审查线。

用法：
    shiftboss audit new      # 建 sweep-project-audit-<日期> 并记下本次审查的 main 提交
    shiftboss audit status   # 连续空闲次数、上次审查提交与是否满足触发条件

**为什么要两次**：一次门铃时恰好没事，可能只是两段工作的间隙；全项目审查是大活，误触发会占满名额。
连续两次门铃（且相隔不少于 `MIN_GAP_MINUTES`）都无事可做才算真空闲。**main 没前进不再审**：
代码没变，重审只会得到同一份结论。审查后的复核（派活方核实问题、复审 codex 判为无问题之处）见 README 3.3。

空闲判据复用 `shiftboss brief` 的结果，不另写一套：该你处理、待你定为无，在跑、排队为 0。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from shiftboss import config
from shiftboss.paths import project_root
from shiftboss.state import read_state, write_state

MIN_GAP_MINUTES = 30
AUDIT_PREFIX = "sweep-project-audit"
TEMPLATE = Path(__file__).parent / "docs" / "templates" / "audit" / "contract.md"


def state_path(remote: Path) -> Path:
    return config.control_dir(remote) / "idle-audit.json"


def idle_now(repo: Path) -> tuple[bool, str]:
    """→（是否无事可做，不空闲的原因）。"""
    out = subprocess.run([sys.executable, "-m", "shiftboss.cli", "brief"], cwd=repo,
                         capture_output=True, text=True).stdout
    checks = [("【该你处理】：无", "有该你处理的事"), ("【待你定】：无", "有待你定的事"),
              ("【在跑】0/", "有在跑的线"), ("【排队】0", "有排队的线")]
    for marker, reason in checks:
        if marker not in out:
            return False, reason
    # Red diagnostics can remain after their line is archived. Brief's actionable
    # items and live/queued counts already describe the work that prevents idling.
    return True, ""


def main_head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "origin/main"], cwd=repo, capture_output=True, text=True).stdout.strip()


def audit_in_progress(repo: Path) -> bool:
    # Empty leftovers do not own a task; use the same taskbook index as dispatch.
    from shiftboss.taskbook import Taskbook
    return any(book.name.startswith(AUDIT_PREFIX) for book in Taskbook.iter_active(repo))


def pending(repo: Path, remote: Path) -> str | None:
    """已触发的审查留在简报待处理项，直到建线；不因门铃消息已读而消失。"""
    if not config.automation("idle_audit", repo):
        return None
    state = read_state(state_path(remote)) or {}
    head = state.get("proposed_head")
    return head if head and head != state.get("audited_head") and not audit_in_progress(repo) else None


def observe(remote: Path) -> str | None:
    """门铃每响一次调一次；满足触发条件时返回要写进门铃的一行，否则返回 None。"""
    repo = project_root()
    if not config.automation("idle_audit", repo):
        return None
    state = read_state(state_path(remote)) or {}
    idle, _reason = idle_now(repo)
    now = time.time()
    if not idle:
        state.update(streak=0, first_idle=None)
        write_state(state_path(remote), state)
        return None
    state["streak"] = state.get("streak", 0) + 1
    state["first_idle"] = state.get("first_idle") or now
    write_state(state_path(remote), state)
    head = main_head(repo)
    if (state["streak"] >= 2 and now - state["first_idle"] >= MIN_GAP_MINUTES * 60
            and head and head != state.get("audited_head") and head != state.get("proposed_head")
            and not audit_in_progress(repo)):
        state.update(proposed_head=head, streak=0, first_idle=None)
        write_state(state_path(remote), state)
        return f"idle-audit 待处理：main@{head[:12]} 满足空闲审查条件；当轮建审查任务书、核对合同、提交推送并派出（README 3.7）"
    return None


def new(repo: Path, remote: Path) -> str:
    from shiftboss import taskbook
    stem = f"{AUDIT_PREFIX}-{datetime.now():%Y%m%d}"
    name = stem
    suffix = 1
    while taskbook.Taskbook.find(repo, name) is not None:
        suffix += 1
        name = f"{stem}-{suffix}"
    taskbook.new(repo, name, kind="sweep")
    references = config.load(repo).get("audit", {}).get("references", [])
    rules = "、".join(f"[{Path(r).name}](../../../{r})" for r in references) or "项目的架构依据与项目说明"
    contract = TEMPLATE.read_text(encoding="utf-8").replace("{name}", name).replace("{rules}", rules)
    plan = repo / "shiftboss" / "plans" / name
    (plan / "contract.md").write_text(contract, encoding="utf-8")
    meta = json.loads((plan / "meta.json").read_text(encoding="utf-8"))
    meta.update({"写入范围": [f"game/evidence/{name}/"], "推理强度": "high", "完成标准": "成形", "破坏性": "否"})
    (plan / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = read_state(state_path(remote)) or {}
    state.update(audited_head=main_head(repo), proposed_head=None, streak=0, first_idle=None, last_audit=name)
    write_state(state_path(remote), state)
    return name


def main(argv: list[str] | None = None) -> int:
    from shiftboss.verify import origin
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["new", "status"])
    args = parser.parse_args(argv)
    repo = project_root()
    remote = origin(repo)
    if args.command == "status":
        state = read_state(state_path(remote)) or {}
        idle, reason = idle_now(repo)
        print(f"当前{'无事可做' if idle else '不空闲：' + reason}｜连续空闲 {state.get('streak', 0)} 次"
              f"｜上次审查 {state.get('last_audit') or '—'}（main@{(state.get('audited_head') or '—')[:12]}）")
        return 0
    name = new(repo, remote)
    print(f"已建 {name}：核对合同后提交、派出；派出后派活方另做独立抽查留作对照（README 3.3 第 13 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
