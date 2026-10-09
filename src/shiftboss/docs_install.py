"""包内通用文档装进项目：执行方只读得到自己副本里的东西，所以规则要进项目仓库。

源在包内 `docs/`，装到项目的 `shiftboss/`（任务书在同目录的 `plans/`，不属于装入的文件）；
装进去的是逐字节副本，项目里不改它们。项目专有的约定写在项目自己的文档里，不写进这个目录。
"""
from __future__ import annotations

from pathlib import Path

from shiftboss.paths import PLANS_DIR, PROJECT_DIR

SOURCE = Path(__file__).resolve().with_name("docs")


def wanted(repo: Path) -> dict[Path, bytes]:
    """项目里应有的文件 → 内容。"""
    target = repo / PROJECT_DIR
    return {target / path.relative_to(SOURCE): path.read_bytes()
            for path in sorted(SOURCE.rglob("*")) if path.is_file()}


def drift(repo: Path, *, missing: bool = True) -> list[str]:
    """与包内源不一致（`missing` 时连同缺失）的文件，相对项目根。源就在项目里（开发期同一路径）时为空。"""
    target = (repo / PROJECT_DIR).resolve()
    if target == SOURCE:
        return []
    return [path.relative_to(repo).as_posix() for path, data in wanted(repo).items()
            if (path.is_file() and path.read_bytes() != data) or (missing and not path.is_file())]


def extra(repo: Path) -> list[str]:
    """项目文档目录里包内已没有的文件：只报告，不删。"""
    target = repo / PROJECT_DIR
    plans = repo / PLANS_DIR
    known = set(wanted(repo))
    return sorted(p.relative_to(repo).as_posix() for p in target.rglob("*")
                  if p.is_file() and p not in known and plans not in p.parents) if target.is_dir() else []


ENTRY_FILES = ("AGENTS.md", "CLAUDE.md")
ENTRY_LINK = f"{PROJECT_DIR}/README.md"
ENTRY_LINE = f"工作流：派活方与执行方的流程规则见 [{ENTRY_LINK}]({ENTRY_LINK})。"


def _points(repo: Path, name: str) -> bool:
    """入口文档已指向流程文档：字面含链接，或 CLAUDE.md 导入了已指向的 AGENTS.md。"""
    text = (repo / name).read_text(encoding="utf-8")
    return ENTRY_LINK in text or (name == "CLAUDE.md" and "@AGENTS.md" in text
                                  and (repo / "AGENTS.md").is_file() and _points(repo, "AGENTS.md"))


def entry_missing(repo: Path) -> list[str]:
    """未指向流程文档的入口文档；两个都不存在时两个都算缺。"""
    present = [name for name in ENTRY_FILES if (repo / name).is_file()]
    return [name for name in present if not _points(repo, name)] if present else list(ENTRY_FILES)


# AI 每次开会话只读入口文档；入口不指向流程文档，派活方与执行方就不知道这套流程。
# 已有的入口追加一行，都没有时新建两个（CLAUDE.md 导入 AGENTS.md，免得两处各写一份）。
def install_entry(repo: Path, check: bool) -> bool:
    missing = entry_missing(repo)
    if not missing:
        return True
    if check:
        print("AI 入口文档未指向流程文档：" + "、".join(missing))
        return False
    for name in missing:
        path = repo / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            path.write_text(text + ("" if text.endswith("\n") else "\n") + f"\n{ENTRY_LINE}\n", encoding="utf-8")
        else:
            path.write_text("@AGENTS.md\n" if name == "CLAUDE.md" else f"{ENTRY_LINE}\n", encoding="utf-8")
    print("AI 入口文档已指向流程文档：" + "、".join(missing))
    return True


def install(repo: Path, check: bool) -> bool:
    stale = drift(repo)
    for name in extra(repo):
        print(f"文档目录里多出（包内已无，原样保留）：{name}")
    if not stale:
        return True
    if check:
        print("工作流文档与包内不一致：" + "、".join(stale))
        return False
    for path, data in wanted(repo).items():
        if path.relative_to(repo).as_posix() in stale:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print(f"装工作流文档进 {PROJECT_DIR}/：{len(stale)} 个文件")
    return True
