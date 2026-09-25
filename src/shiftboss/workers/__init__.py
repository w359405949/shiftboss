"""执行方插件：每个执行方一个模块，接口相同；派活、恢复与收件箱只经这里取执行方。

包内自带 `codex`、`agy`、`omp`；另装的执行方经 entry points 组 `shiftboss.workers` 注册（名字 → 模块）。

每个模块提供：

- `PROCESS_NAMES`：它的 TUI 进程名；
- `CAN_QUEUE`：能否往活会话里投消息；不能的，会话停在等输入时由派活方关掉再带交代恢复；
- `SESSION_WAIT`：派出后等新会话号的秒数；
- `tui_command(copy, prompt, resume_id=None)`：起 TUI 的 argv，工作目录由启动方设为副本；
- `configured(argv)`：这个进程是否按派活方式（权限全开）起的；
- `newest_session(copy, since, previous, previous_steps)`：派出后这个副本上在用的会话号；
- `step_count(session_id)`：会话已有几步，供恢复判断接上与否；
- `parked_at(session_id)`：会话停在等输入上的时刻，在干活或读不到为 None；
- `model_of(session_id)`：实际使用的模型；
- `queue_into(session_id, message, alive)`：投消息，返回是否真的投进去（`CAN_QUEUE` 为假的一律 False）；
- `sandbox_warning(session_id)`：会话权限不是全开时的告警，否则 None。
"""
from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from types import ModuleType

BUILTIN = ("codex", "agy", "omp")


def _load() -> dict[str, ModuleType]:
    workers = {name: importlib.import_module(f"shiftboss.workers.{name}") for name in BUILTIN}
    for point in entry_points(group="shiftboss.workers"):
        workers.setdefault(point.name, point.load())
    return workers


WORKERS = _load()


def default() -> str:
    """任务书没写执行方时用哪个：`shiftboss.toml` 的 `[workers] default`，缺省 codex。"""
    from shiftboss.config import default_worker
    return default_worker()


def get(name: str) -> ModuleType:
    return WORKERS[name]
