"""任务交付前的项目快检清单；命令只从项目已登记项解析。"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from shiftboss import config
from shiftboss.taskbook import Taskbook, FAST_CHECKS_KEY, dependency_list


class TaskChecksError(ValueError):
    pass


def registry(repo: Path) -> dict[str, dict]:
    result = {}
    for item in config.fast_commands(repo):
        if not isinstance(item, dict):
            raise TaskChecksError('merge.fast_commands 每项必须是具名检查对象')
        if not isinstance(item.get('background', False), bool):
            raise TaskChecksError('merge.fast_commands.background 必须是布尔值')
        name = item.get('name')
        run = item.get('run')
        if not isinstance(name, str) or not name.strip() or name in result:
            raise TaskChecksError('merge.fast_commands 的 name 必须非空且唯一')
        if run != 'regen.closure_command' and (not isinstance(run, list) or not run
                or not run[0] or any(not isinstance(arg, str) for arg in run)):
            raise TaskChecksError(f'快速检查 {name} 的 run 必须是非空参数列表')
        when = item.get('when')
        if when is not None and (not isinstance(when, list)
                or any(not isinstance(prefix, str) or not prefix for prefix in when)):
            raise TaskChecksError(f'快速检查 {name} 的 when 必须是路径前缀列表')
        result[name] = item
    return result


def selected(repo: Path, book: Taskbook, value=None) -> list[dict]:
    if value is None:
        value = book.meta().get(FAST_CHECKS_KEY)
    if value is None:
        raise TaskChecksError(f'{book.name} 未声明「{FAST_CHECKS_KEY}」；'
                              '派活方用 taskbook set 填已登记检查名，无额外项目快检填 []')
    if not isinstance(value, (list, str)):
        raise TaskChecksError(f'{FAST_CHECKS_KEY}必须是检查名列表')
    names = dependency_list(value)
    if len(names) != len(set(names)):
        raise TaskChecksError(f'{FAST_CHECKS_KEY}不能重复登记同一检查')
    available = registry(repo)
    items = []
    for name in names:
        if name not in available:
            raise TaskChecksError(f'{book.name} 的快速检查未登记：{name}；先核对项目 merge.fast_commands')
        item = available[name]
        if item.get('background', False):
            raise TaskChecksError(f'{name} 已移入常驻验证，不列入任务交付前快速检查')
        items.append(item)
    return items


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)


def task_for_branch(repo: Path) -> Taskbook | None:
    books = list(Taskbook.iter_active(repo))
    if not books:
        return None
    branch = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    if branch.returncode:
        return None
    matches = [b for b in books if b.meta().get('分支', f'codex/{b.name}') == branch.stdout.strip()]
    if len(matches) > 1:
        raise TaskChecksError('同一分支对应多份任务书，不能确定快速检查清单')
    return matches[0] if matches else None


def task_for_target(repo: Path, target: str, task: str | None = None) -> Taskbook | None:
    """冻结提交优先按精确分支 tip 识别；中间提交只接受唯一归属。"""
    books = list(Taskbook.iter_active(repo))
    if not books:
        if task:
            raise TaskChecksError(f'没有在办任务书：{task}')
        return None
    refs = _git(repo, 'for-each-ref', '--format=%(refname) %(objectname)',
                'refs/remotes/origin/', 'refs/heads/')
    contained = _git(repo, 'for-each-ref', '--format=%(refname)',
                     '--contains=' + target, 'refs/remotes/origin/', 'refs/heads/')
    if refs.returncode or contained.returncode:
        raise TaskChecksError(f'无法确认合入提交的任务归属：{refs.stderr or contained.stderr}')
    tips = dict(line.rsplit(' ', 1) for line in refs.stdout.splitlines())
    containing_refs = set(contained.stdout.splitlines())
    found = []
    for book in books:
        branch = book.meta().get('分支', f'codex/{book.name}')
        remote = 'refs/remotes/origin/' + branch
        ref = remote if remote in tips else 'refs/heads/' + branch
        if ref in containing_refs:
            found.append((book, tips[ref] == target))
        elif remote in tips and 'refs/heads/' + branch in containing_refs:
            raise TaskChecksError(f'{book.name} 的提交尚未出现在远端任务分支；先确认推送并 fetch')
    if task:
        requested = next((book for book, _ in found if book.name == task), None)
        if requested is None:
            raise TaskChecksError(f'{task} 的分支不包含合入提交 {target[:12]}')
        return requested
    exact = [book for book, same in found if same]
    candidates = exact or [book for book, _ in found]
    if len(candidates) > 1:
        raise TaskChecksError('合入提交对应多份任务书；用 merge --task <任务书名> 明确快检清单')
    return candidates[0] if candidates else None


def resolve(repo: Path, task: str | None = None) -> Taskbook | None:
    if task:
        book = Taskbook.find(repo, task)
        if book is None:
            raise TaskChecksError(f'没有这份任务书：{task}')
        current = task_for_branch(repo)
        if current and current.name != book.name:
            raise TaskChecksError(f'当前分支属于 {current.name}，不能使用 {book.name} 的快检清单')
        return book
    return task_for_branch(repo)


def commands(repo: Path, task: str | None = None) -> tuple[Taskbook | None, list[dict]]:
    book = resolve(repo, task)
    return book, selected(repo, book) if book else list(registry(repo).values())


def matches(item: dict, paths: list[str]) -> bool:
    return not item.get('when') or any(path.startswith(prefix) for path in paths if path
                                      for prefix in item['when'])


def describe(repo: Path, task: str | None = None, paths: list[str] | None = None) -> str:
    book, items = commands(repo, task)
    lines = [f'交付前快速检查：{book.name if book else "非任务分支（项目默认）"}',
             '共同结构守卫：shiftboss check 固定执行；任务清单不能关闭。']
    for item in items:
        where = '后台，不在交付前执行' if item.get('background', False) else '前台'
        if paths is not None and not matches(item, paths):
            where = '本次改动未命中 when，不执行'
        run = item['run']
        command = run if isinstance(run, str) else shlex.join(run)
        lines.append(f'- {item["name"]}（{where}）：{command}')
    if not items:
        lines.append('无额外项目快检脚本（快速检查：[]）。')
    return '\n'.join(lines)
