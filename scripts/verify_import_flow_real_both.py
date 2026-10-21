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

# 期望笔数【不硬编码】——从预览响应里派生。
# 硬编码会把脚本绑死在某一份样本上：换一份真实账单（哪怕只是多导了一个月）
# 断言就必然失败，看起来像导入坏了，其实是断言写死了。
# 派生出来的断言反而更强：它校验的是「预览看到的 = 写入的 = 重复导入跳过的」，
# 这条等式对任何数据都成立。


def require(ok: bool, message: str) -> None:
    if not ok:
        raise AssertionError(message)


def upload(client: TestClient, endpoint: str, file: Path) -> dict:
    with file.open("rb") as stream:
        response = client.post(endpoint, files={"file": (file.name, stream)})
    # 不打印响应正文：预览正文可能包含私人原始账单行。
    require(response.status_code == 200,
            f"{endpoint} 返回 HTTP {response.status_code}。请在本地排查，不要公开响应正文。")
    return response.json()


def import_once(client: TestClient, file: Path, count: int, label: str) -> dict:
    response = upload(client, "/api/import/commit", file)
    # 不打印响应正文：可能包含私人原始账单行。
    require((response["inserted"], response["skipped"]) == (count, 0),
            f"{label} 首次导入结果不符合预期（期望新增 {count} 笔，"
            f"实际新增 {response['inserted']} / 跳过 {response['skipped']}）")
    return response


def import_again(client: TestClient, file: Path, count: int, label: str) -> dict:
    response = upload(client, "/api/import/commit", file)
    require((response["inserted"], response["skipped"]) == (0, count),
            f"{label} 重复导入结果不符合预期（期望跳过 {count} 笔，"
            f"实际新增 {response['inserted']} / 跳过 {response['skipped']}）")
    return response


def main() -> None:
    wx_files = list(PRIVATE.glob("微信支付账单流水文件*.xlsx"))
    al_files = list(PRIVATE.glob("支付宝交易明细*.csv"))
    require(len(wx_files) == 1 and len(al_files) == 1,
            "请确保 private_input 下恰好有 1 份微信 XLSX 和 1 份支付宝 CSV，并检查文件名。")
    wx, al = wx_files[0], al_files[0]
    original = db.DB_PATH

    try:
        with TemporaryDirectory(prefix="finance_real_both_isolated_") as tmp:
            db.DB_PATH = Path(tmp) / "isolated_test.db"
            with TestClient(app) as client:
                require(client.get("/health").json()["transactions"] == 0,
                        "独立测试库未保持为空")
                print("1/8  独立临时数据库已创建；正式 bills.db 不参与测试")

                ap = upload(client, "/api/import/preview", al)
                require(ap["platform_id"] == "alipay", "支付宝文件未被识别为 alipay")
                require(client.get("/health").json()["transactions"] == 0,
                        "预览不应向数据库写入记录")
                n_alipay = ap["total"]
                require(n_alipay > 0, "支付宝预览没有解析出任何有效交易")
                print(f"2/8  支付宝真实 CSV 预览通过：{n_alipay} 笔有效交易，预览未写库")

                first_al = import_once(client, al, n_alipay, "支付宝")
                print(f"3/8  支付宝首次导入通过：新增 {first_al['inserted']} 笔"
                      f"（解析失败 {first_al['failed']} 笔）")
                import_again(client, al, n_alipay, "支付宝")
                print(f"4/8  支付宝重复导入通过：新增 0 笔，跳过 {n_alipay} 笔")

                wp = upload(client, "/api/import/preview", wx)
                require(wp["platform_id"] == "wechat", "微信文件未被识别为 wechat")
                n_wechat = wp["total"]
                require(n_wechat > 0, "微信预览没有解析出任何有效交易")
                first_wx = import_once(client, wx, n_wechat, "微信")
                print(f"5/8  微信预览与首次导入通过：新增 {first_wx['inserted']} 笔"
                      f"（解析失败 {first_wx['failed']} 笔）")
                import_again(client, wx, n_wechat, "微信")
                print(f"6/8  微信重复导入通过：新增 0 笔，跳过 {n_wechat} 笔")

                conn = db.get_conn()
                try:
                    counts = {row["platform_id"]: row["n"] for row in conn.execute(
                        "SELECT platform_id, COUNT(*) AS n FROM transactions_raw GROUP BY platform_id"
                    )}
                    raw_count = conn.execute(
                        "SELECT COUNT(*) FROM transactions_raw WHERE raw_json IS NOT NULL AND raw_json != ''"
                    ).fetchone()[0]
                    refund_count = conn.execute(
                        "SELECT COUNT(*) FROM transactions_raw WHERE platform_id='alipay' "
                        "AND (status LIKE '%退款%' OR tx_type LIKE '%退款%')"
                    ).fetchone()[0]
                finally:
                    conn.close()
                require(counts == {"alipay": n_alipay, "wechat": n_wechat},
                        "跨平台统一数据库笔数异常")
                require(raw_count == n_alipay + n_wechat,
                        "原始 JSON 未完整保留")
                print(f"7/8  统一数据库核对通过：支付宝 {counts['alipay']} + 微信 {counts['wechat']} = {raw_count} 笔；支付宝退款相关 {refund_count} 笔")

                hist = client.get("/api/import/history").json()
                require(len(hist["batches"]) == 4, "预期应有 4 个导入批次")
                require(client.get("/health").json()["transactions"] == raw_count,
                        "后端统计与数据库笔数不一致")
                print("8/8  导入历史与数据库统计一致：4 个导入批次")
                print("\n8/8 检查通过！使用的是真实账单，且仅写入临时数据库；正式 db/bills.db 未修改。")
    finally:
        db.DB_PATH = original


if __name__ == "__main__":
    main()
