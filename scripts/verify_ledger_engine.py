"""一键验收第二轮 Ledger 引擎。

只使用 sample_data/支付宝格式_引擎验收_虚构数据.csv，并在临时目录建立独立数据库；
不会读取、覆盖或修改项目的 db/bills.db。
运行：python scripts/verify_ledger_engine.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.importer import import_file  # noqa: E402
from backend.ledger import service  # noqa: E402

CSV_PATH = ROOT / "sample_data" / "支付宝格式_引擎验收_虚构数据.csv"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def near(actual: float, expected: float, label: str) -> None:
    check(round(float(actual), 2) == round(float(expected), 2),
          f"{label}：实际 {actual}，预期 {expected}")


def main() -> int:
    if not CSV_PATH.exists():
        print(f"找不到测试账单：{CSV_PATH}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="ledger_engine_acceptance_") as temp:
        # 关键安全措施：覆盖本进程的 DB_PATH，验收不触碰用户真实数据库。
        db.DB_PATH = Path(temp) / "acceptance.db"
        db.init_db(db.DB_PATH)

        imported = import_file(CSV_PATH, platform_id="alipay")
        check(imported.inserted == 13, f"导入笔数不符：{imported.inserted}，预期 13")
        check(imported.skipped == 0, f"测试库不应有重复记录：{imported.skipped}")

        conn = db.get_conn()
        ext = conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609100010'").fetchone()
        exp = conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609010001'").fetchone()
        inc = conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609040004'").fetchone()
        failed = conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609140014'").fetchone()
        check(ext is not None and exp is not None and inc is not None, "测试账单关键交易未导入")
        check(failed is None, "交易关闭的记录应在导入时被过滤")
        ext_id, exp_id, inc_id = int(ext["id"]), int(exp["id"]), int(inc["id"])
        conn.execute("INSERT INTO tx_overrides(tx_id, funding, updated_by) VALUES(?, '亲情卡', 'acceptance')",
                     (ext_id,))
        conn.commit()
        conn.close()

        base = service.get_live_tree()
        near(base["root_e"], 216.75, "基础总支出")
        near(base["root_i"], 201.20, "基础总收入")
        near(base["root_net"], -15.55, "基础净额")
        check(base["count"] == 13, "账本交易笔数应为 13")
        check(base["invariants_ok"] and base.get("audit_ok") and len(base["invariants"]) == 6, "不变量/审计链检查未全部通过")
        check(len(base["refund_pairs"]) == 2, "全额/部分退款关联数量应为 2")
        partial = next(tx for tx in base["transactions"] if tx["tx_no"] == "T202609120012")
        near(partial["amount"], 50.00, "部分退款原始金额")
        near(partial["refunded_amount"], 10.00, "部分退款累计金额")
        near(partial["effective_amount"], 40.00, "部分退款有效金额")
        check(len(base["internal_transfer_pairs"]) == 1, "自有账户转账配对数量应为 1")
        check(any("部分退款" in warning for warning in base["warnings"]), "应提示部分退款已按剩余金额处理")
        print("[PASS] 支付宝格式 CSV 导入、方向规则、全额/部分退款抵扣、孤立退款、内部转账")

        split = service.apply_op("star-live", {"op": "split", "tx_id": ext_id}, base["seq"])
        near(split["root_e"], 216.75, "分裂后总支出")
        near(split["root_i"], 321.20, "分裂后总收入")
        near(split["root_net"], 104.45, "分裂后净额")
        mirror = next((leg for leg in split["legs"] if leg["tx_id"] == ext_id and leg["role"] == "mirror"), None)
        check(mirror is not None, "外部额度交易缺少镜像腿")
        near(mirror["e"], 0.00, "镜像腿支出")
        near(mirror["i"], 120.00, "镜像腿收入")
        check(split["invariants_ok"] and split.get("audit_ok"), "分裂后不变量或审计链未通过")
        print("[PASS] 外部额度 split、主腿/镜像腿对称、外部收入节点、不变量校验")

        undone = service.undo("star-live", split["seq"])
        near(undone["root_net"], -15.55, "撤销分裂后的净额")
        check(undone["split_count"] == 0, "撤销分裂后分裂状态应清空")
        redone = service.redo("star-live", undone["seq"])
        near(redone["root_net"], 104.45, "重做分裂后的净额")
        unsplit = service.apply_op("star-live", {"op": "unsplit", "tx_id": ext_id}, redone["seq"])
        near(unsplit["root_net"], -15.55, "unsplit 后净额")
        check(unsplit["split_count"] == 0 and unsplit["invariants_ok"], "unsplit 状态或不变量不正确")
        print("[PASS] undo / redo / unsplit 历史往返")

        grouped = service.apply_op("star-live", {
            "op": "create_node", "parent_id": unsplit["root_id"], "name": "验收 NET 组",
            "op_value": "NET", "tx_ids": [exp_id, inc_id],
        }, unsplit["seq"])
        net_node = next(node for node in grouped["nodes"] if node["name"] == "验收 NET 组")
        near(net_node["e"], 0.00, "NET 节点支出")
        near(net_node["i"], 164.50, "NET 节点收入")
        near(grouped["root_net"], -15.55, "NET 分组前后根净额")
        check(grouped["invariants_ok"], "NET 分组后不变量未通过")
        print("[PASS] ADD/NET 递归聚合与根净额恒等")

        print("\n所有引擎验收项均通过。")
        print("测试数据：13 笔已导入；1 笔交易关闭记录被过滤。")
        print("最终数据库位于临时目录，程序退出后自动删除；真实 db/bills.db 未被修改。")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
