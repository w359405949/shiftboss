"""Record a reviewed red-page result awaiting new evidence, without clearing its failure.

    python -m shiftboss.page_followups ROUTE --reason TEXT --evidence REVIEW.json

Only the current complete red-result identity is acknowledged, for at most 24 hours.
A new result, changed/missing original or review, or expiry restores the reminder.
This does not change patrol results, claims, checks, baselines, or page acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import time

from shiftboss import config
from shiftboss.state import read_state, write_state


def store(remote: Path) -> Path:
    return config.control_dir(remote) / 'patrol' / 'reviewed-results.json'


def file_identity(path: Path) -> list[int]:
    stat = path.stat()
    if not path.is_file():
        raise ValueError(f'Not a result file: {path}')
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def record(remote: Path, route: str, reason: str, evidence: Path) -> dict:
    if not reason.strip():
        raise ValueError('A concrete reason for awaiting new evidence is required')
    state = read_state(config.control_dir(remote) / 'patrol' / 'state.json') or {}
    result = state.get('red', {}).get(route)
    if not result or any(key not in result for key in ('first', 'last', 'commit', 'log', 'code', 'reasons')):
        raise ValueError('Route has no complete current red-result identity')
    if not result['code']:
        raise ValueError('Route is not red')
    evidence = evidence.resolve(strict=True)
    review_identity = file_identity(evidence)
    review_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    if file_identity(evidence) != review_identity:
        raise ValueError('Review changed while recording its identity')
    item = {'result': result, 'logIdentity': file_identity(Path(result['log'])),
            'reason': reason.strip(), 'evidence': str(evidence), 'evidenceIdentity': review_identity,
            'evidenceSha256': review_sha, 'recorded': time.time(), 'expires': time.time() + 24 * 3600}
    current = read_state(config.control_dir(remote) / 'patrol' / 'state.json') or {}
    if current.get('red', {}).get(route) != result:
        raise ValueError('A new result arrived; review it before recording a follow-up')
    entries = read_state(store(remote)) or {}
    entries[route] = item
    write_state(store(remote), entries)
    return item


def matches(item: dict | None, result: dict) -> bool:
    if not item or item.get('result') != result or not item.get('reason') or item.get('expires', 0) <= time.time():
        return False
    try:
        return (file_identity(Path(result['log'])) == item['logIdentity']
                and file_identity(Path(item['evidence'])) == item['evidenceIdentity'])
    except (OSError, KeyError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    from shiftboss.paths import project_root
    from shiftboss.verify import origin
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('route')
    parser.add_argument('--reason', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args(argv)
    item = record(origin(project_root()), args.route, args.reason, args.evidence)
    print(f"已审，待新证据：{args.route}；红项保留；依据 {item['evidence']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
