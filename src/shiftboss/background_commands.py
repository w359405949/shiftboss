"""Existing slow source rules registered in resident verification, without a new runner."""
from pathlib import Path
from shiftboss import config

def commands(root: Path | None = None) -> list[list[str]]:
    """迁移的是既有规则的执行位置；常驻执行完整 argv，不按本次 changed 选区跳过。"""
    commands = []
    for item in config.fast_commands(root):
        flag = item.get('background', False)
        if not isinstance(flag, bool):
            raise ValueError('merge.fast_commands.background 必须是布尔值')
        if flag:
            run = item['run']
            if not isinstance(run, list) or not run or not all(isinstance(x, str) for x in run):
                raise ValueError('后台结构检查必须登记完整 argv')
            commands.append(list(run))
    return commands

