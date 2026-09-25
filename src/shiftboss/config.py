"""项目配置 `shiftboss.toml`：项目专有的名字与路径只从这里进包。缺文件或缺项时取中性缺省值。"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

from shiftboss.paths import project_root

CONFIG_NAME = "shiftboss.toml"


@lru_cache(maxsize=None)
def load(root: Path | None = None) -> dict:
    path = (root or project_root()) / CONFIG_NAME
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _get(section: str, key: str, default, root: Path | None = None):
    value = load(root).get(section, {}).get(key, default)
    return value


def plan_file(root: Path | None = None) -> str:
    """生成计划（步骤、闸、分层、后台验证策略）所在文件，相对项目根。"""
    return _get("regen", "plan", "regen.json", root)


def catalog_file(root: Path | None = None) -> str:
    """生产端产物清单，相对项目根。"""
    return _get("regen", "catalog", "derived-artifacts.json", root)


def local_config_file(root: Path | None = None) -> str | None:
    """不进 git 的本机配置文件名；项目没有这种文件时为 None。"""
    return _get("project", "local_config", None, root)


def js_module_prefixes(root: Path | None = None) -> list[str]:
    """合并时按 ES 模块检查语法的路径前缀。"""
    return list(_get("merge", "js_module_prefixes", [], root))


def assertion_dirs(root: Path | None = None) -> list[str]:
    """合并时列出断言增删的测试目录。"""
    return list(_get("merge", "assertion_dirs", [], root))


def docs_dir(root: Path | None = None) -> str:
    """包内通用文档装进项目的哪个目录（相对项目根）。"""
    return _get("project", "docs_dir", "docs/shiftboss", root)


def default_worker(root: Path | None = None) -> str:
    """任务书没写执行方时派给谁。"""
    return _get("workers", "default", "codex", root)


HUB_NAME = "hub.git"


def copies_dir(bare: Path, root: Path | None = None) -> Path:
    """shiftboss 的家目录 `<项目名>.shiftboss/`：bare（`hub.git`）、控制目录、执行方副本与验证工作区都在这里。

    缺省从 bare 的位置认：bare 叫 `hub.git` 时就是它所在的目录；否则是 bare 旁的 `<名>.shiftboss/`。
    """
    value = _get("project", "copies_dir", None, root)
    if value:
        return ((root or project_root()) / value).resolve()
    bare = Path(bare)
    if bare.name == HUB_NAME:
        return bare.parent
    return bare.parent / (bare.name.removesuffix(".git") + ".shiftboss")


def control_dir(bare: Path, root: Path | None = None) -> Path:
    """仅派活方读写的控制目录（私有文档、台账、门铃、验证报告）；缺省为副本目录下的 `control/`。"""
    value = _get("project", "control_dir", None, root)
    path = (root or project_root()) / value if value else copies_dir(bare, root) / "control"
    path.mkdir(parents=True, exist_ok=True)
    return path
