"""一次性建表：`python -m backend.ledger2.migrate --db <路径>`。必须显式给路径，不默认指向真库。"""
from __future__ import annotations

import argparse
import sys
from contextlib import closing

from .store import connect, ensure_schema


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='在指定数据库里创建 ledger2_* 表（幂等）。')
    ap.add_argument('--db', required=True, help='数据库文件路径（必填；不会默认使用真库）')
    args = ap.parse_args(argv)
    with closing(connect(args.db)) as conn:
        ensure_schema(conn)
        n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ledger2_%' AND type='table'").fetchone()[0]
    sys.stdout.write(f'ledger2 表就绪：{n} 张（{args.db}）\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
