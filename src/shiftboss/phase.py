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
    """摘要保留进度的原始更新时间，不用简报生成时间冒充重新估算。"""
    if current.status in ("unset", "cleared"):
        return ["【当前阶段】：未设置" if current.status == "unset" else "【当前阶段】：已清空"]
    if current.status == "error":
        return ["【当前阶段】：读取失败", f"- {current.path}｜{current.error}"]
    lines = [line.strip() for line in current.text.splitlines() if line.strip()]
    title = lines[0].lstrip("# ")

    def field(name: str) -> str:
        return next((line.split(separator, 1)[1].strip()
                     for line in lines for separator in ("：", ":")
                     if line.startswith(name + separator)), "")

    def short(value: str) -> str:
        return value if len(value) <= 160 else value[:159] + "…"

    result = [f"【当前阶段】{short(title)}"]
    if focus := field("重心"):
        result.append(f"- 重心｜{short(focus)}")
    elif goal := field("目标"):
        result.append(f"- 目标｜{short(goal)}")
    if priority := field("当务之急"):
        result.append(f"- 当务之急｜{short(priority)}")
    if progress := field("进度"):
        result.append(f"- 进度｜{short(progress)}")
        result.append(f"- 更新于｜{short(field('更新时间') or '未填写（不是实时进度）')}")
        if basis := field("依据"):
            result.append(f"- 依据｜{short(basis)}")
    result.append(f"- 完整文档｜{current.path}")
    return result
