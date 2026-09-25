"""包内通用文档装进项目：执行方只读得到自己副本里的东西，所以规则要进项目仓库。

源在包内 `docs/`，装到项目的 `docs_dir`（`shiftboss.toml`，缺省 `docs/shiftboss/`）；装进去的是逐字节副本，
项目里不改它们。项目专有的约定写在项目自己的文档里，不写进这个目录。
"""
from __future__ import annotations

from pathlib import Path

from shiftboss import config

SOURCE = Path(__file__).resolve().with_name("docs")


def wanted(repo: Path) -> dict[Path, bytes]:
    """项目里应有的文件 → 内容。"""
    target = repo / config.docs_dir(repo)
    return {target / path.relative_to(SOURCE): path.read_bytes()
            for path in sorted(SOURCE.rglob("*")) if path.is_file()}


def drift(repo: Path, *, missing: bool = True) -> list[str]:
    """与包内源不一致（`missing` 时连同缺失）的文件，相对项目根。源就在项目里（开发期同一路径）时为空。"""
    target = (repo / config.docs_dir(repo)).resolve()
    if target == SOURCE:
        return []
    return [path.relative_to(repo).as_posix() for path, data in wanted(repo).items()
            if (path.is_file() and path.read_bytes() != data) or (missing and not path.is_file())]


def extra(repo: Path) -> list[str]:
    """项目文档目录里包内已没有的文件：只报告，不删。"""
    target = repo / config.docs_dir(repo)
    known = set(wanted(repo))
    return sorted(p.relative_to(repo).as_posix() for p in target.rglob("*")
                  if p.is_file() and p not in known) if target.is_dir() else []


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
    print(f"装工作流文档进 {config.docs_dir(repo)}/：{len(stale)} 个文件")
    return True
