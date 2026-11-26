"""Generate ATLAS walkthrough statements from fictional records, with no input files."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook


ALIPAY_HEADERS = (
    "收/支", "交易时间", "交易分类", "交易对方", "商品说明", "金额",
    "收/付款方式", "交易状态", "交易订单号", "商家订单号", "备注",
)
WECHAT_HEADERS = (
    "交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)",
    "支付方式", "当前状态", "交易单号", "商户单号", "备注",
)


@dataclass(frozen=True)
class Entry:
    time: str
    category: str
    merchant: str
    item: str
    amount: str
    direction: str = "支出"


# A meal can have a non-food platform label. One November book purchase is at noon.
ALIPAY_DEMO = (
    Entry("2026-10-03 12:00:00", "校园服务", "虚构校园食堂", "十月午餐套餐", "18.00"),
    Entry("2026-10-17 18:30:00", "餐饮", "虚构圆桌餐厅", "十月朋友聚餐", "120.00"),
    Entry("2026-10-20 09:00:00", "交通", "虚构公交站", "十月乘车充值", "30.00"),
    Entry("2026-10-01 09:15:00", "补助", "虚构奖助中心", "十月学习补助", "1000.00", "收入"),
    Entry("2026-11-03 12:10:00", "校园服务", "虚构校园食堂", "十一月食堂午饭", "18.00"),
    Entry("2026-11-08 13:40:00", "餐饮", "虚构米饭小馆", "十一月晚到的午饭", "24.00"),
    Entry("2026-11-13 15:30:00", "餐饮", "虚构烘焙店", "十一月下午零食", "10.00"),
    Entry("2026-11-07 12:35:00", "学习用品", "虚构纸页书店", "十一月午间买书", "16.00"),
    Entry("2026-11-01 09:15:00", "补助", "虚构奖助中心", "十一月学习补助", "1100.00", "收入"),
)
WECHAT_DEMO = (
    Entry("2026-10-06 18:10:00", "商户消费", "虚构面馆", "十月晚饭", "42.00"),
    Entry("2026-10-12 15:20:00", "商户消费", "虚构点心铺", "十月下午零食", "20.00"),
    Entry("2026-10-22 10:30:00", "商户消费", "虚构文具屋", "十月文具套装", "70.00"),
    Entry("2026-11-05 12:45:00", "商户消费", "虚构饭团铺", "十一月饭团午餐", "22.00"),
    Entry("2026-11-09 18:20:00", "商户消费", "虚构面馆", "十一月晚饭", "46.00"),
    Entry("2026-11-16 18:45:00", "商户消费", "虚构圆桌餐厅", "十一月朋友聚餐", "120.00"),
    Entry("2026-11-22 10:30:00", "商户消费", "虚构文具屋", "十一月学习用品", "54.00"),
)
INVALID = Entry("2026-11-29 10:00:00", "餐饮", "虚构测试店", "无效金额样例", "not-a-number")


def write_alipay(path: Path, entries: Iterable[Entry], batch: str) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["支付宝交易记录明细查询（虚构教学数据）"])
        writer.writerow(ALIPAY_HEADERS)
        for number, row in enumerate(entries, 1):
            tx_id = f"atlas-{batch}-ali-{number:04d}"
            writer.writerow([
                row.direction, row.time, row.category, row.merchant, row.item, row.amount,
                "余额", "交易成功", tx_id, f"order-{tx_id}", "虚构教学数据",
            ])


def write_wechat(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "虚构微信支付账单"
    sheet.append(["微信支付账单明细（虚构教学数据）"])
    sheet.append(WECHAT_HEADERS)
    for number, row in enumerate(WECHAT_DEMO, 1):
        tx_id = f"atlas-demo-wx-{number:04d}"
        sheet.append([
            row.time, row.category, row.merchant, row.item, row.direction, float(row.amount),
            "零钱", "支付成功", tx_id, f"order-{tx_id}", "虚构教学数据",
        ])
    with path.open("xb") as stream:
        workbook.save(stream)
    workbook.close()


def volume_entries() -> Iterable[Entry]:
    templates = (
        ("交通", "虚构车站", "区间车票", "8.00"),
        ("餐饮", "虚构烘焙店", "茶点", "12.50"),
        ("餐饮", "虚构米饭小馆", "单人套餐", "18.00"),
        ("学习用品", "虚构文具屋", "学习用品", "26.00"),
        ("日用", "虚构百货店", "日用品", "35.50"),
    )
    start = datetime(2026, 10, 1, 8)
    for i in range(3000):
        timestamp = start + timedelta(days=i // 50, minutes=13 * (i % 50))
        yield Entry(timestamp.isoformat(sep=" "), *templates[(i + i // 50) % len(templates)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="output directory for fictional statements")
    parser.add_argument("--volume", action="store_true", help="only generate the 3,000-row Alipay case")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    filenames = ("alipay-3000.csv",) if args.volume else (
        "alipay-demo.csv", "wechat-demo.xlsx", "alipay-mixed.csv", "alipay-invalid.csv",
    )
    paths = [args.out / name for name in filenames]
    if any(path.exists() for path in paths):
        parser.error("output file already exists; choose another --out directory")
    if args.volume:
        write_alipay(paths[0], volume_entries(), "volume")
    else:
        write_alipay(paths[0], ALIPAY_DEMO, "demo")
        write_wechat(paths[1])
        write_alipay(paths[2], (
            Entry("2026-11-29 09:00:00", "交通", "虚构公交站", "有效车票样例", "4.00"), INVALID,
        ), "mixed")
        write_alipay(paths[3], (INVALID,), "invalid")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
