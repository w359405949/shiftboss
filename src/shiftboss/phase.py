"""甲方当前阶段的目标与工作取舍；不是任务台账或项目架构。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

NAME = "PHASE.md"
MAX_DOCUMENT_BYTES = 256_000


@dataclass(frozen=True)
class Phase:
    path: Path
    status: str
    text: str = ""
    sha256: str | None = None
    size: int = 0
    error: str | None = None

    def identity(self) -> dict:
        return {"path": str(self.path), "status": self.status, "sha256": self.sha256,
                "bytes": self.size, "error": self.error}


def read(directory: Path) -> Phase:
    """每次重读原件；清空或删除即撤下旧目标，不缓存摘要。"""
    path = directory / NAME
    try:
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("阶段文档指向 control 外，未读取")
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError("阶段文档超过读取上限；须精简或手动读取")
        text = raw.decode("utf-8")
    except FileNotFoundError:
        return Phase(path, "unset")
    except (OSError, ValueError) as error:
        return Phase(path, "error", error=str(error))
    return Phase(path, "active" if text.strip() else "cleared", text,
                 hashlib.sha256(raw).hexdigest(), len(raw))


def summary(current: Phase) -> list[str]:
    """简报只显示标题、目标与原件入口，完整策略仍在阶段文档。"""
    if current.status in ("unset", "cleared"):
        return ["【当前阶段】：未设置" if current.status == "unset" else "【当前阶段】：已清空"]
    if current.status == "error":
        return ["【当前阶段】：读取失败", f"- {current.path}｜{current.error}"]
    lines = [line.strip() for line in current.text.splitlines() if line.strip()]
    title = lines[0].lstrip("# ")
    goal = next((line.split(separator, 1)[1].strip()
                 for line in lines for separator in ("：", ":")
                 if line.startswith("目标" + separator)), "")

    def short(value: str) -> str:
        return value if len(value) <= 160 else value[:159] + "…"

    return [f"【当前阶段】{short(title)}", *([f"- 目标｜{short(goal)}"] if goal else []),
            f"- 完整文档｜{current.path}"]
