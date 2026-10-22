"""
backend/reports/ —— 只读查询组件（7 个 preset 报告 + 待分类清单）

硬约束
======
本包【只有 SELECT】。不允许 INSERT / UPDATE / DELETE。
理由：账本可信的前提之一是「报告只读，不改数据」。

全部查询 v_tx 视图，不直接 JOIN —— 修正层以后加字段改视图即可，
本包一行都不用动。

数值约定见 docs/设计/第一轮/生成报告.md 第 5 节。
"""
from .summary import get_summary
from .calendar import get_calendar
from .trend import get_trend
from .category import get_by_category
from .method import get_by_method
from .income_rank import get_income_rank
from .transactions import query_transactions, list_pending

__all__ = [
    "get_summary",
    "get_calendar",
    "get_trend",
    "get_by_category",
    "get_by_method",
    "get_income_rank",
    "query_transactions",
    "list_pending",
]
