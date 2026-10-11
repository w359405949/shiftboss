"""项目配置 `shiftboss.toml`：项目专有的名字与路径只从这里进包。缺项时取中性缺省值；缺文件时 `dispatch` 拒派。

各项的含义见下方各读取函数；派活方生成这份文件时以它们为清单。
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

from shiftboss.paths import project_root

CONFIG_NAME = "shiftboss.toml"


def missing(root: Path | None = None) -> Path | None:
    """缺配置文件时返回它应在的路径，否则 None。"""
    path = (root or project_root()) / CONFIG_NAME
    return None if path.exists() else path


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


def closure_command(root: Path | None = None) -> list[str]:
    """项目算各阶段代码读取闭包的命令；标准输出为 `{阶段名: [路径, …]}` 的 JSON。不配则只用声明的输入。"""
    return list(_get("regen", "closure_command", [], root))


def catalog_file(root: Path | None = None) -> str:
    """生产端产物清单，相对项目根。"""
    return _get("regen", "catalog", "derived-artifacts.json", root)


def local_config_file(root: Path | None = None) -> str | None:
    """不进 git 的本机配置文件名；项目没有这种文件时为 None。"""
    return _get("project", "local_config", None, root)


def js_module_prefixes(root: Path | None = None) -> list[str]:
    """合并时按 ES 模块检查语法的路径前缀。"""
    return list(_get("merge", "js_module_prefixes", [], root))


def js_parser(root: Path | None = None) -> str | None:
    """合并时检查对象重复键所用的 acorn ES 模块路径（相对项目根）；不配则跳过该项。"""
    return _get("merge", "js_parser", None, root)


def assertion_dirs(root: Path | None = None) -> list[str]:
    """合并时列出断言增删的测试目录。"""
    return list(_get("merge", "assertion_dirs", [], root))


def approved_tests(root: Path | None = None) -> list[str]:
    """经用户同意新增的测试项：测试文件路径、`路径::用例名`，或闸命令里新出现的参数。"""
    return list(_get("verify", "approved_tests", [], root))


def verify_budget_minutes(root: Path | None = None) -> float | None:
    """常驻验证一轮（生成＋全部闸）的时间预算；超出即报红。不配则不查。"""
    return _get("verify", "budget_minutes", None, root)


def default_worker(root: Path | None = None) -> str:
    """任务书没写执行方时派给谁。"""
    return _get("workers", "default", "codex", root)


HUB_NAME = "hub.git"
VERIFICATION_NAME = "verification"  # 控制目录下常驻验证的状态目录


def copies_dir(bare: Path, root: Path | None = None) -> Path:
    """shiftboss 的家目录 `<项目名>.shiftboss/`：bare（`hub.git`）、控制目录、执行方副本与验证工作区都在这里。

    缺省从 bare 的位置认：bare 叫 `hub.git` 时就是它所在的目录；否则是 bare 旁的 `<名>.shiftboss/`。
    """
    value = _get("project", "copies_dir", None, root)
    if value:
        return ((root or project_root()) / value).resolve()
    return default_control_dir(bare).parent


def default_control_dir(bare: Path) -> Path:
    """未配置 `copies_dir`、`control_dir` 时的控制目录；只凭 bare 的位置认，hub 钩子里没有项目根也能用。"""
    bare = Path(bare)
    home = bare.parent if bare.name == HUB_NAME else bare.parent / (bare.name.removesuffix(".git") + ".shiftboss")
    return home / "control"


def control_dir(bare: Path, root: Path | None = None) -> Path:
    """仅派活方读写的控制目录（私有文档、台账、门铃、验证报告）；缺省为副本目录下的 `control/`。"""
    value = _get("project", "control_dir", None, root)
    if value:
        path = (root or project_root()) / value
    elif _get("project", "copies_dir", None, root):
        path = copies_dir(bare, root) / "control"
    else:
        path = default_control_dir(bare)
    path.mkdir(parents=True, exist_ok=True)
    return path
