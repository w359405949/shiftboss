"""按逐条处理清单整理私有错题本；先完整归档，再移除已落实的条目。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def reconcile(control: Path, repo: Path, plan_path: Path, apply: bool = False) -> dict:
    source = control / 'mistakes.md'
    original = source.read_bytes()
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes.decode('utf-8'))
    digest = hashlib.sha256(original).hexdigest()
    if plan.get('source_sha256') != digest:
        raise ValueError('错题本已变化；重新读取并更新处理清单，不能按旧清单删除')
    count = len(re.findall(r'^## ', original.decode('utf-8'), re.M))
    if plan.get('entry_count') != count:
        raise ValueError('处理清单的条目数与错题本不符')
    accounted = []
    pending = []
    groups = plan.get('groups', [])
    keys = set()
    for group in groups:
        key = group['key']
        if key in keys:
            raise ValueError('机制分组重复')
        keys.add(key)
        ids = group['ids']
        if not ids or any(type(i) is not int for i in ids):
            raise ValueError('每组须列出原错题序号')
        accounted.extend(ids)
        if group['action'] not in ('script', 'documentation', 'existing', 'pending'):
            raise ValueError('未知处理动作')
        if not group.get('decision') or not group.get('basis'):
            raise ValueError('缺少处理结论或落实依据')
        for reference in group['basis']:
            path = Path(reference.split('#', 1)[0])
            if not (path if path.is_absolute() else repo / path).is_file():
                raise ValueError(f'落实依据不存在：{reference}')
        if group['action'] == 'pending':
            item = plan.get('pending', {}).get(key, {})
            if not item.get('owner') or not item.get('completion'):
                raise ValueError('未落实项须有负责人和具体完成条件')
            pending.append((group, item))
    if sorted(accounted) != list(range(1, count + 1)):
        raise ValueError('原条目必须逐条覆盖且只能归入一组；禁止遗漏、重复或越号')
    report = {'original_entries': count, 'groups': len(groups),
              'resolved_entries': count - sum(len(g['ids']) for g, _ in pending),
              'pending_groups': len(pending), 'applied': apply}
    if not apply:
        return report
    archive = control / 'mistakes-archive' / f'reconciliation-{digest[:16]}'
    archive.mkdir(parents=True, exist_ok=True)
    for name, body in [('original.md', original), ('plan.json', plan_bytes)]:
        path = archive / name
        if path.exists() and path.read_bytes() != body:
            raise ValueError(f'已有归档不同，拒绝覆盖：{path}')
        if not path.exists():
            with path.open('xb') as file:
                file.write(body)
    active = ['# 错题本', '',
              '只保留尚未落实的差错。新增仍按日期标题、出错方、错在哪、发现与处理记录。', '',
              f'本批完整原文与逐条处理依据：`{archive.relative_to(control)}/`。',
              '归档表示处理规则已落实，不表示历史页面、业务或发布后补核通过。', '']
    for group, item in pending:
        active += [f"## 待落实：{group['key']}", '',
                   f"- 原条目：{', '.join(map(str, group['ids']))}",
                   f"- 错在哪：{group['decision']}", f"- 负责：{item['owner']}",
                   f"- 完成条件：{item['completion']}", '']
    if source.read_bytes() != original:
        raise ValueError('归档期间错题本已变化，保留原件，不执行清空')
    temporary = source.with_name('mistakes.reconciled.tmp')
    temporary.write_text('\n'.join(active), encoding='utf-8')
    os.replace(temporary, source)
    return {**report, 'archive': str(archive)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path, help='含来源摘要、逐条序号、落实依据和未完成负责人的 JSON 清单')
    parser.add_argument('--apply', action='store_true', help='默认仅核对；显式应用才归档并整理')
    args = parser.parse_args(argv)
    from shiftboss.paths import project_root
    from shiftboss.inbox import origin_git_dir
    from shiftboss.config import control_dir
    try:
        repo = project_root()
        report = reconcile(control_dir(origin_git_dir(repo)), repo, args.plan, args.apply)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'错题整理拒绝：{error}', file=sys.stderr)
        return 2
