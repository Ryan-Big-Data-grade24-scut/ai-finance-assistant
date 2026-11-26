"""Row checks shared by the WeChat and Alipay payment exports."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Iterable, Iterator

from .base import DIRECTION_MAP, ParseIssues, Row, clean, norm_amount, norm_direction, norm_time, should_drop


_RANGE_FIELD = re.compile(r"(起始|开始|终止|结束)(?:时间|日期)\s*[:：]\s*\[([^\]]+)\]")
_ORDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_STATUS = {
    "交易成功", "支付成功", "交易完成", "付款成功", "收款成功", "转账成功", "还款成功", "提现成功",
    "退款成功", "退款完成", "已退款", "部分退款", "已部分退款", "全额退款", "已全额退款", "已全额退",
    "已付款", "已支付", "已完成", "已收款", "已转账", "已收钱", "朋友已收钱", "对方已收钱",
    "已领取", "已被领取", "已提现", "已转入", "已转出", "已到账", "资金已到账", "提现已到账", "已入账", "已存入", "已存入零钱",
    "已退还", "已退回", "已还款", "充值成功", "存入成功", "转入成功", "转出成功", "已存入零钱通",
}
_STATUS_SUFFIX = {"已退款", "退款成功", "部分退款", "已部分退款", "全额退款", "已全额退款"}
_DIRECTION = set(DIRECTION_MAP) - {""} | {"中性", "不计入收支"}


def _bounds(metadata: Iterable[str]) -> tuple[str, str] | None:
    times = {}
    for text in metadata:
        for label, raw in _RANGE_FIELD.findall(text):
            start = label in ("起始", "开始")
            raw = raw.strip()
            value = norm_time(raw) or norm_time(raw + (" 00:00:00" if start else " 23:59:59"))
            if value:
                times[start] = value
    if True in times and False in times and times[True] <= times[False]:
        return times[True], times[False]
    return None


def _known_status(status: str) -> bool:
    base, separator, qualifier = status.replace("（", "(").replace("）", ")").partition("(")
    return base.strip() in _STATUS and (
        not separator or (qualifier.endswith(")") and qualifier[:-1].strip() in _STATUS_SUFFIX)
    )


class ExportRows:
    def __init__(self, columns: dict[str, int], metadata: Iterable[str], issues: ParseIssues | None):
        self.columns = columns
        self.range = _bounds(metadata)
        self.now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        self.issues = issues
        self.errors: dict[int, str] = {}
        self.rows: list[tuple[int, Row]] = []

    def check(self, line: int, get: Callable[[str], object]) -> tuple[str, float, str, str] | None:
        status = clean(get("status"))
        # Known failed or closed transactions have never been imported as settled rows.
        if should_drop(status):
            return None
        problems = []
        tx_time = norm_time(get("tx_time") or get("payment_time"))
        amount = norm_amount(get("amount"), exact=True)
        direction = str(get("direction") or "").strip()
        tx_no = clean(get("tx_no"))

        if tx_time is None:
            problems.append("交易时间缺失或无效")
        elif tx_time > self.now:
            problems.append("交易时间在未来")
        elif self.range and not self.range[0] <= tx_time <= self.range[1]:
            problems.append("交易时间超出账单声明区间")

        if amount is None:
            reason = "金额精度超过两位小数" if norm_amount(get("amount")) is not None else "金额无效或不大于零"
            problems.append(reason)
        if direction not in _DIRECTION:
            problems.append("收支方向缺失或无法识别")
        if "tx_type" in self.columns and not clean(get("tx_type")):
            problems.append("缺少交易类型或分类")
        if "status" in self.columns and not _known_status(status):
            problems.append("交易状态缺失或无法识别")
        if "tx_no" in self.columns and not _ORDER_ID.fullmatch(tx_no):
            problems.append("交易单号缺失或格式无效")
        if problems:
            self.errors[line] = "；".join(problems)
            return None
        assert tx_time is not None and amount is not None
        return tx_time, amount, norm_direction(direction), status

    def accepted(self) -> Iterator[Row]:
        orders: dict[str, list[tuple[int, Row]]] = defaultdict(list)
        for line, row in self.rows:
            if row.tx_no:
                orders[row.tx_no].append((line, row))
        for records in orders.values():
            if len(records) < 2:
                continue
            first = records[0][1]
            conflict = any(row != first for _, row in records[1:])
            for line, _ in records:
                self.errors[line] = "文件内交易单号重复且记录冲突" if conflict else "文件内交易记录重复"
        if self.issues is not None:
            for line, reason in sorted(self.errors.items()):
                self.issues.add(line, reason)
        for line, row in self.rows:
            if line not in self.errors:
                yield row
