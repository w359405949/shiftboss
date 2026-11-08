"""压缩／恢复后从权威文件补回甲方规则与当前状态，不创建第二份交接本。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

from shiftboss import phase
from shiftboss.hooks.auto_recover import authorized
from shiftboss.state import read_state, write_state

MODULE = "shiftboss.hooks.context_refresh"
MAX_BYTES = 16_000
DOCUMENT_BYTES = 256_000
WORKFLOW_SECTIONS = ("2.1", "5.1", "2.0", "4.3")


def section(text: str, number: str) -> str | None:
    headings = list(re.finditer(r"^(#{1,6})\s+(\S+).*?$", text, re.MULTILINE))
    for index, heading in enumerate(headings):
        if heading[2] != number:
            continue
        end = next((h.start() for h in headings[index + 1:] if len(h[1]) <= len(heading[1])), len(text))
        return text[heading.start():end].strip()
    return None


def build(repo: Path, directory: Path) -> tuple[str, dict]:
    """只读取一次磁盘现场；超限保留原文件入口，不切掉半条规则。"""
    parts = [
        "ShiftBoss 上下文恢复：以下内容重新读取自当前磁盘，不采用压缩摘要中的旧状态。\n"
        f"当前阶段目标见 {directory / phase.NAME}；本次原件未完整注入时，先读取全文。"
        "阶段结束或用户调整时更新或清空它，不沿用旧阶段策略。"
        "继续合并、派发、归档或清理前：先读取 AGENTS.md、README.md 与本次相关任务的"
        "当前 contract／最新轮次／delivery；运行一次 inbox list（读全含⚠）和 brief，"
        "涉及异常恢复再用 recover --check。以下台账未关闭不等于实际在跑，"
        "旧通知、旧诊断和发布状态须按当前来源核对。最新用户指令优先。"
    ]
    record = {"documents": [], "omitted": [], "errors": []}

    def add(label: str, text: str) -> bool:
        block = f"\n\n[{label}]\n{text}"
        if len(("".join(parts) + block).encode("utf-8")) <= MAX_BYTES - 1000:
            parts.append(block)
            return True
        else:
            record["omitted"].append(label)
            return False

    # 阶段目标先于台账和规则正文，避免任务多时把当前工作取舍挤出上下文。
    current = phase.read(directory)
    record["phase"] = current.identity()
    if current.status == "active":
        record["documents"].append({"path": str(current.path), "sha256": current.sha256})
        record["phase"]["fullyEmitted"] = add(f"当前阶段目标：{current.path}", current.text.strip())
    elif current.status == "error":
        record["errors"].append(f"{current.path}：{current.error}")
        add("当前阶段目标", f"读取失败：{current.path}；相关操作前重新读取，不按旧摘要推断目标。")
    else:
        add("当前阶段目标", f"{current.path}：{'未设置' if current.status == 'unset' else '已清空'}；不沿用旧阶段目标。")

    # 先保留小的实时入口；不在钩子中启动耗时收件箱或探测执行方。
    sessions = read_state(directory / "sessions.json")
    from shiftboss.taskbook import Taskbook
    rows = []
    for name, entry in sessions.items():
        if not isinstance(entry, dict) or entry.get("closed"):
            continue
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
            raise ValueError("会话台账含非法任务名")
        book = Taskbook.find(repo, name)
        if not book or book.archived:
            rows.append(f"{name}：台账未关但无在办任务书；先核对，不恢复或重派。")
            continue
        meta = book.meta()
        rows.append(f"{name}：第{meta.get('轮次', '?')}轮，副本={meta.get('你的工作副本') or '未分配'}；"
                    f"快速检查={json.dumps(meta.get('快速检查'), ensure_ascii=False)}；"
                    f"当前任务入口={book.rel(repo)}")
    add("台账未关闭的任务（不认证执行状态）", "\n".join(rows) or "无。仍需收件箱确认停下与待核对项。")
    todos = read_state(directory / "todo.json").get("items", [])
    add("在途待办入口", f"{directory / 'todo.json'}：{len(todos)}项；"
        "用 brief／todo list 读当前条件，不按历史门铃重复登记。")
    index = read_state(directory / "verification/index.json")
    add("正常验证快照（不等于产品通过）", json.dumps(
        {key: index.get(key) for key in ("active", "last", "last_consistent")}, ensure_ascii=False))
    add("其他实时入口", f"用户裁定：{directory / 'decisions.json'}；"
        f"差错规则：{directory / 'mistakes.md'}；待核对和页面归属由 inbox／brief 现算。"
        "不读历史会话、归档报告或全部日志重建现场。")

    for relative in ("AGENTS.md", "README.md", "shiftboss/README.md"):
        path = repo / relative
        if not path.resolve().is_relative_to(repo.resolve()):
            record["errors"].append(f"{relative} 指向项目外，未注入")
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(DOCUMENT_BYTES + 1)
            if len(raw) > DOCUMENT_BYTES:
                raise ValueError("超过文档读取上限，须按入口读取相关段落")
            text = raw.decode("utf-8")
        except (OSError, ValueError) as error:
            record["errors"].append(f"{relative}：{error}")
            continue
        record["documents"].append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest()})
        if relative != "shiftboss/README.md":
            add(relative, text.strip())
            continue
        for number in WORKFLOW_SECTIONS:
            selected = section(text, number)
            if selected is None:
                record["errors"].append(f"{relative} 缺第{number}节；须重新读当前目录定位")
            else:
                add(f"{relative} 第{number}节原文", selected)
    if record["omitted"] or record["errors"]:
        # 入口列表本身也有上限；超大文档不挤掉“刷新不完整”的明确提示。
        details = json.dumps({k: record[k] for k in ("omitted", "errors")}, ensure_ascii=False)
        details = details.encode("utf-8")[:700].decode("utf-8", errors="ignore")
        parts.append("\n\n刷新未完整注入的入口（相关操作前重新读取）：" + details)
    context = "".join(parts)
    assert len(context.encode("utf-8")) <= MAX_BYTES
    record["contextBytes"] = len(context.encode("utf-8"))
    return context, record


def refresh(repo: Path, payload: dict) -> dict | None:
    if (payload.get("hook_event_name") != "SessionStart"
            or payload.get("source") not in ("compact", "resume")
            or not authorized(repo, payload)):
        return None
    from shiftboss.config import control_dir
    from shiftboss.inbox import origin_git_dir
    directory = control_dir(origin_git_dir(repo), repo)
    context, record = build(repo, directory)
    record.update(event="SessionStart", source=payload["source"], session=payload["session_id"],
                  at=datetime.now(timezone.utc).isoformat(), status="emitted")
    write_state(directory / "context-refresh.json", record)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def install(repo: Path, path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    before = json.dumps(data, sort_keys=True)
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    argv = [sys.executable, "-B", "-m", MODULE, "--repo", str(repo)]
    command = subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)
    # 只安装一条 SessionStart，避免 PostCompact 与 compact 重复注入。
    for group in groups:
        group["hooks"] = [hook for hook in group.get("hooks", []) if MODULE not in hook.get("command", "")]
    groups[:] = [group for group in groups if group.get("hooks")]
    groups.append({"matcher": "compact|resume", "hooks": [{"type": "command", "command": command,
        "timeout": 10, "additionalContextLimit": 8000, "statusMessage": "重读协作规则与当前工作入口"}]})
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
    parser.add_argument("--install-hooks", type=Path)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.install_hooks:
        print("已安装上下文恢复钩子。" if install(repo, args.install_hooks) else "上下文恢复钩子已是当前配置。")
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        result = refresh(repo, payload)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(f"上下文刷新失败：{error}；继续操作前手动读取权威规则与 inbox/brief。", file=sys.stderr)
        return 1
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
