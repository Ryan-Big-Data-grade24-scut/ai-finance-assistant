"""
backend/importer/base.py —— 导入器基类与归一化工具

设计要点：
    1. 归一化是【索引】不是【替代品】—— 原始整行永远存进 raw_json
    2. dedupe_key 优先用平台交易单号，没有则退回 MD5 指纹
    3. 所有平台共用同一套派生字段推导（date/month/year/weekday/hour）
    4. status 为「已关闭 / 失败」的行直接丢弃，不入库
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

# ── 状态过滤：这些交易不该进账本 ──────────────────────────────
STATUS_DROP = ("交易关闭", "已关闭", "支付失败", "还款失败", "已取消", "转账关闭")

# ── 方向归一化 ────────────────────────────────────────────────
# 微信用 '/' 表示中性，支付宝用 '不计收支'，两个都要认
DIRECTION_MAP = {
    "支出": "支出",
    "收入": "收入",
    "/": "中性",
    "": "中性",
    "不计收支": "中性",
    "其他": "中性",
    " neutral": "中性",
}

# ── 空值占位符：各平台用它表示「无」 ─────────────────────────
EMPTY_TOKENS = {"/", "", "-", "null", "None", "\\"}


@dataclass
class Row:
    """一笔归一化后的交易，对应 transactions_raw 的一行。"""

    platform_id: str
    source: str
    tx_time: str            # 'YYYY-MM-DD HH:MM:SS'
    direction: str          # 支出 / 收入 / 中性
    amount: float
    pay_method: str = ""
    counterparty: str = ""
    item: str = ""
    tx_type: str = ""
    status: str = ""
    tx_no: str = ""
    merchant_no: str = ""
    remark: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    # ── 派生字段 ────────────────────────────────────────────
    @property
    def date(self) -> str:
        return self.tx_time[:10]

    @property
    def month(self) -> str:
        return self.tx_time[:7]

    @property
    def year(self) -> str:
        return self.tx_time[:4]

    @property
    def weekday(self) -> int:
        """0=周一 … 6=周日。datetime.weekday() 正好是 0=周一。"""
        return datetime.strptime(self.tx_time, "%Y-%m-%d %H:%M:%S").weekday()

    @property
    def hour(self) -> int:
        return int(self.tx_time[11:13])

    @property
    def dedupe_key(self) -> str:
        """去重指纹。

        优先用平台交易单号（唯一可靠）。
        没有单号时退回「平台+时间+金额+对方」的 MD5 —— 但这种方案有误判风险：
        同一时刻同一金额同一对手方的两笔真实交易会被当成重复。
        所以 ImportCommitResp.skipped 必须暴露给用户看。
        """
        if self.tx_no:
            return f"{self.source}|{self.tx_no}"
        sig = f"{self.source}|{self.tx_time}|{self.amount}|{self.counterparty}"
        return f"{self.source}|md5:{hashlib.md5(sig.encode('utf-8')).hexdigest()[:16]}"

    def to_sql_tuple(self, batch_id: str) -> tuple:
        return (
            self.source, self.platform_id, self.tx_time, self.direction,
            self.amount, self.pay_method, self.counterparty, self.item,
            self.tx_type, self.status, self.tx_no, self.merchant_no,
            self.remark, self.date, self.month, self.year,
            self.weekday, self.hour, self.dedupe_key,
            json.dumps(self.raw, ensure_ascii=False), batch_id,
        )


@dataclass
class ParseIssues:
    """解析阶段的数据质量统计；不会包含用户交易字段值。"""

    failed: int = 0
    first_error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def add(self, line_number: int, reason: str) -> None:
        message = f"第 {line_number} 行：{reason}"
        self.failed += 1
        if self.first_error is None:
            self.first_error = message
        if len(self.warnings) < 5:
            self.warnings.append(message)


INSERT_SQL = """
INSERT INTO transactions_raw
  (source, platform_id, tx_time, direction, amount, pay_method,
   counterparty, item, tx_type, status, tx_no, merchant_no, remark,
   date, month, year, weekday, hour, dedupe_key, raw_json, import_batch)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(dedupe_key) DO NOTHING
"""


# ── 归一化工具 ────────────────────────────────────────────────

def clean(value: Any) -> str:
    """把各平台的空值占位符统一成空字符串。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s in EMPTY_TOKENS else s


def norm_direction(value: Any) -> str:
    """归一化收支方向。认 '/' 和 '不计收支' 为中性。"""
    return DIRECTION_MAP.get(clean(value), "中性")


def norm_amount(value: Any) -> float | None:
    """金额归一化：仅接收有限正数，统一保留两位小数。"""
    s = clean(value).replace("¥", "").replace("￥", "").replace(",", "").replace("元", "")
    if not s:
        return None
    try:
        amount = Decimal(s)
        if not amount.is_finite():
            return None
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount <= 0:
            return None
        return float(amount)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def norm_time(value: Any) -> str | None:
    """时间归一化到 'YYYY-MM-DD HH:MM:SS'。

    微信给的是 datetime 对象，支付宝给的是 'YYYY-MM-DD HH:MM:SS' 字符串。
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    s = clean(value)
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def should_drop(status: Any) -> bool:
    """状态为关闭/失败的交易直接丢弃。"""
    s = clean(status)
    return any(token in s for token in STATUS_DROP)


class PlatformAdapter:
    """平台适配器基类。

    新增平台时继承它，实现 name / platform_id / parse 即可。
    绝大多数 CSV 类平台根本不用写代码，用 generic_csv + YAML 就够。
    """

    name: str = ""
    platform_id: str = ""

    def parse(self, path) -> Iterator[Row]:  # pragma: no cover - 抽象
        raise NotImplementedError
