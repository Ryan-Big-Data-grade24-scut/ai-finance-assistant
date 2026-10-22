# -*- coding: utf-8 -*-
"""真实微信 + 支付宝 CSV 双平台完整导入验证。

仅通过 FastAPI TestClient 操作临时数据库；不连接正在运行的服务，
不写 db/bills.db，不打印任何交易内容、金额或身份信息。
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

# 把脚本放到项目 scripts/ 下，从任意当前工作目录都能定位项目。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from backend import db  # noqa: E402
from backend.main import app  # noqa: E402

PRIVATE = ROOT / "private_input"


def require(ok: bool, message: str) -> None:
    if not ok:
        raise AssertionError(message)


def upload(client: TestClient, endpoint: str, file: Path) -> dict:
    with file.open("rb") as stream:
        response = client.post(endpoint, files={"file": (file.name, stream)})
    # 不打印响应正文：预览正文可能包含私人原始账单行。
    require(
        response.status_code == 200,
        f"{endpoint} 返回 HTTP {response.status_code}。请在本地排查，不要公开响应正文。",
    )
    return response.json()


def preview_count(preview: dict, platform_id: str, label: str) -> int:
    """以本次预览结果为基准，不假设每个人的账单笔数相同。"""
    require(
        preview.get("platform_id") == platform_id,
        f"{label} 预览识别平台错误",
    )
    count = preview.get("total")
    require(
        isinstance(count, int) and count > 0,
        f"{label} 预览未返回大于 0 的有效交易数量",
    )
    return count


def import_once(client: TestClient, file: Path, count: int, label: str) -> dict:
    response = upload(client, "/api/import/commit", file)
    inserted = response["inserted"]
    skipped = response["skipped"]
    failed = response["failed"]

    # preview.total 只统计有效交易。commit 还可能显式报告无效行
    # （例如 amount <= 0）；failed 不计入有效交易的 inserted/skipped 等式。
    require(
        inserted + skipped == count and isinstance(failed, int) and failed >= 0,
        f"{label} 首次导入未完整处理预览中的有效交易，或失败计数无效",
    )
    require(
        inserted > 0,
        f"{label} 首次导入没有新增记录；请检查样本是否全为重复交易",
    )
    return response


def import_again(
    client: TestClient,
    file: Path,
    count: int,
    label: str,
    expected_failed: int,
) -> dict:
    response = upload(client, "/api/import/commit", file)
    inserted = response["inserted"]
    skipped = response["skipped"]
    failed = response["failed"]

    # 重复导入时，有效交易应全部跳过；无效行仍须显式报告 failed。
    require(
        inserted == 0 and skipped == count,
        f"{label} 重复导入的有效交易结果不符合预期",
    )
    require(
        isinstance(failed, int) and failed >= 0,
        f"{label} 重复导入返回了无效的失败计数",
    )
    require(
        failed == expected_failed,
        f"{label} 重复导入的失败计数与首次导入不一致",
    )
    return response


def main() -> None:
    wx_files = list(PRIVATE.glob("微信支付账单流水文件*.xlsx"))
    al_files = list(PRIVATE.glob("支付宝交易明细*.csv"))
    require(
        len(wx_files) == 1 and len(al_files) == 1,
        "请确保 private_input 下恰好有 1 份微信 XLSX 和 1 份支付宝 CSV，并检查文件名。",
    )
    wx, al = wx_files[0], al_files[0]
    original = db.DB_PATH

    try:
        with TemporaryDirectory(prefix="finance_real_both_isolated_") as tmp:
            db.DB_PATH = Path(tmp) / "isolated_test.db"
            with TestClient(app) as client:
                require(
                    client.get("/health").json()["transactions"] == 0,
                    "独立测试库未保持为空",
                )
                print("1/8  独立临时数据库已创建；正式 bills.db 不参与测试")

                ap = upload(client, "/api/import/preview", al)
                al_count = preview_count(ap, "alipay", "支付宝")
                require(
                    client.get("/health").json()["transactions"] == 0,
                    "预览不应向数据库写入记录",
                )
                print(
                    f"2/8  支付宝真实 CSV 预览通过：{al_count} 笔有效交易，预览未写库"
                )

                al_first = import_once(client, al, al_count, "支付宝")
                print(
                    f"3/8  支付宝首次导入通过：新增 {al_first['inserted']} 笔，"
                    f"跳过有效交易 {al_first['skipped']} 笔，"
                    f"显式报告失败记录 {al_first['failed']} 笔"
                )
                al_again = import_again(
                    client, al, al_count, "支付宝", al_first["failed"]
                )
                require(
                    client.get("/health").json()["transactions"]
                    == al_first["inserted"],
                    "支付宝重复导入不应新增数据库记录",
                )
                print(
                    f"4/8  支付宝重复导入通过：新增 0 笔，"
                    f"跳过有效交易 {al_count} 笔，"
                    f"显式报告失败记录 {al_again['failed']} 笔"
                )

                wp = upload(client, "/api/import/preview", wx)
                wx_count = preview_count(wp, "wechat", "微信")
                require(
                    client.get("/health").json()["transactions"]
                    == al_first["inserted"],
                    "微信预览不应向数据库写入记录",
                )
                print(
                    f"5/8  微信真实 XLSX 预览通过：{wx_count} 笔有效交易，预览未写库"
                )

                wx_first = import_once(client, wx, wx_count, "微信")
                wx_again = import_again(
                    client, wx, wx_count, "微信", wx_first["failed"]
                )
                require(
                    client.get("/health").json()["transactions"]
                    == al_first["inserted"] + wx_first["inserted"],
                    "微信重复导入不应新增数据库记录",
                )
                print(
                    f"6/8  微信首次与重复导入通过：首次新增 {wx_first['inserted']} 笔，"
                    f"首次跳过有效交易 {wx_first['skipped']} 笔、失败记录 "
                    f"{wx_first['failed']} 笔；重复导入新增 0 笔，"
                    f"跳过有效交易 {wx_count} 笔、失败记录 {wx_again['failed']} 笔"
                )

                conn = db.get_conn()
                try:
                    counts = {
                        row["platform_id"]: row["n"]
                        for row in conn.execute(
                            "SELECT platform_id, COUNT(*) AS n "
                            "FROM transactions_raw GROUP BY platform_id"
                        )
                    }
                    raw_count = conn.execute(
                        "SELECT COUNT(*) FROM transactions_raw "
                        "WHERE raw_json IS NOT NULL AND raw_json != ''"
                    ).fetchone()[0]
                    refund_count = conn.execute(
                        "SELECT COUNT(*) FROM transactions_raw "
                        "WHERE platform_id='alipay' "
                        "AND (status LIKE '%退款%' OR tx_type LIKE '%退款%')"
                    ).fetchone()[0]
                finally:
                    conn.close()

                # 与预览有效交易数对账，避免仅与 commit 响应对账而漏检漏写。
                expected_counts = {
                    "alipay": al_count,
                    "wechat": wx_count,
                }
                require(
                    counts == expected_counts,
                    "数据库内各平台笔数与预览有效交易数不一致",
                )
                expected_raw_count = al_count + wx_count
                require(
                    raw_count == expected_raw_count,
                    "原始 JSON 未完整保留",
                )
                print(
                    f"7/8  统一数据库核对通过：支付宝 {counts['alipay']} + "
                    f"微信 {counts['wechat']} = {raw_count} 笔；"
                    f"支付宝退款相关 {refund_count} 笔"
                )

                hist = client.get("/api/import/history").json()
                require(
                    len(hist["batches"]) == 4,
                    "预期应有 4 个导入批次",
                )
                require(
                    client.get("/health").json()["transactions"] == raw_count,
                    "后端统计与数据库笔数不一致",
                )
                print("8/8  导入历史与数据库统计一致：4 个导入批次")
                print(
                    "\n8/8 检查通过！使用的是真实账单，且仅写入临时数据库；"
                    "正式 db/bills.db 未修改。"
                )
    finally:
        db.DB_PATH = original


if __name__ == "__main__":
    main()
