"""Derive report-only intake streaks from main's Git history; no counters to drift."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
import re
import subprocess

from shiftboss import config

CODE_SUFFIXES = ('py', 'ts', 'tsx', 'mts', 'cts', 'js', 'jsx', 'mjs', 'cjs',
                 'go', 'rs', 'c', 'h', 'cpp', 'hpp', 'cs', 'java', 'kt', 'swift',
                 'rb', 'php', 'vue', 'svelte', 'html', 'css', 'scss', 'sh', 'sql')
DELIVERY = re.compile(r'^shiftboss/plans/[^/]+/delivery\.md$')


def _git(git_dir: Path, *args: str) -> str:
    result = subprocess.run(['git', f'--git-dir={git_dir}', *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Cannot read main history')
    return result.stdout


@lru_cache(maxsize=16)
def _snapshot(git_dir: Path, head: str) -> dict:
    # Git object ids identify commits; no independent SHA calculation or checking.
    code_paths = [f':(glob)**/*.{suffix}' for suffix in CODE_SUFFIXES]
    code_paths += [':(exclude)shiftboss/**', ':(exclude)docs/**',
                   ':(exclude)out/**', ':(exclude)vendor/**']
    last = _git(git_dir, 'log', '--first-parent', '--diff-merges=first-parent',
                '-1', '--format=%H %ct', head, '--', *code_paths).strip().split()
    last_code = last[0] if last else None
    span = f'{last_code}..{head}' if last_code else head
    changes = _git(git_dir, 'log', '--first-parent', '--diff-merges=first-parent',
                   '--no-renames', '--diff-filter=AM', '--format=COMMIT %H',
                   '--name-only', span, '--', ':(glob)shiftboss/plans/*/delivery.md')
    # One intake may update several task deliveries; count each once per commit.
    count = 0
    paths: set[str] = set()
    for line in changes.splitlines():
        if line.startswith('COMMIT '):
            count += len(paths)
            paths.clear()
        elif DELIVERY.fullmatch(line):
            paths.add(line)
    count += len(paths)
    return {'report_only_intakes': count, 'last_code_commit': last_code,
            'last_code_time': int(last[1]) if last else None}


def snapshot(git_dir: Path) -> dict:
    """Count accepted deliveries since the last actual source change on main."""
    head = _git(git_dir, 'rev-parse', '--verify', 'refs/heads/main').strip()
    return dict(_snapshot(git_dir.resolve(), head))


def summary(repo: Path, git_dir: Path) -> list[str]:
    if not config.automation('delivery_progress', repo):
        return ['【交付推进：关】']
    try:
        threshold = config.report_only_warning_after(repo)
        current = snapshot(git_dir)
    except (ValueError, OSError) as error:
        return ['【交付推进：开】', f'- 计数不可用｜{error}']
    count = current['report_only_intakes']
    lines = ['【交付推进：开】', f'- 连续仅报告合入｜{count} 次（告警阈值 {threshold}）']
    when = current['last_code_time']
    lines.append('- 最近源码合入｜' + (datetime.fromtimestamp(when).strftime('%m-%d %H:%M')
                                      if when is not None else '尚无记录'))
    if count >= threshold:
        lines.append('- ⚠ 源码集成停滞｜连续交回未带来源码合入；先处理阻塞，停止重复取证；判断不清时用 ask 提请用户决定。')
    return lines
