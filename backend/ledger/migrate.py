"""一次性迁移：为已有账本回填当前版本的 ledger_legs 结构。"""
from __future__ import annotations

import json
import sys

from .service import migrate_ledger_legs


def main() -> None:
    result = migrate_ledger_legs()
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
