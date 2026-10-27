"""
tests/test_ledger_http_contract.py —— HTTP 层的**契约**测试

**为什么单独有这一份**：
    第一版骨架里，前端把 `book_id` 塞进 body、后端按 query 收 ——
    结果「撤销 / 重做 / 回到 M₀」三个按钮**全是坏的**：
    后端 422，前端只显示一句「请求失败」，两边都不报错。
    单元测试全绿（引擎没问题），护栏也全绿（护栏只查了参数**名**，
    没查参数**放在哪**）。

    所以这里补两件事：
      ① 用 TestClient 把 8 个端点**按前端 api/ledger.ts 的原样**调一遍
         —— 形状不对就红。
      ② 护栏：前端 postJson 的 payload 键，必须是后端 body 模型的字段；
         后端的 POST 路由不许有「必填的 query 参数」。

    ⚠️ 这一份不测业务，只测**接口形状**。业务在 test_ledger_contract.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import db as db_mod                # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 几笔合成交易 + TestClient。"""
    path = tmp_path / "http_contract.db"
    db_mod.init_db(path)
    monkeypatch.setattr(db_mod, "DB_PATH", path)

    conn = db_mod.get_conn(path)
    try:
        conn.execute(
            "INSERT INTO platforms (id, name, kind, adapter) "
            "VALUES ('test', '测试平台', 'wallet', 'generic_csv')"
        )
        for i in range(1, 5):
            conn.execute(
                "INSERT INTO transactions_raw "
                "(source, platform_id, tx_time, direction, amount, pay_method, "
                " counterparty, item, tx_type, status, tx_no, merchant_no, raw_json, "
                " date, month, year, weekday, hour, dedupe_key) "
                "VALUES ('测试平台', 'test', ?, ?, ?, '支付宝', '测试对手方', ?, ?, "
                "        '交易成功', ?, ?, '{}', '2026-10-05', '2026-10', '2026', 0, 12, ?)",
                (f"2026-10-05 12:00:0{i}", "支出" if i % 2 else "收入",
                 10.0 * i, f"项目{i}", "商户消费",
                 f"no{i}", f"m{i}", f"http-{i}"),
            )
        conn.commit()
    finally:
        conn.close()

    from fastapi.testclient import TestClient

    from backend.main import app

    return TestClient(app)


def test_all_ledger_endpoints_answer_with_frontend_shapes(client):
    """8 个端点全部按**前端 api/ledger.ts 的写法**调一遍。"""
    # ① 建账（body）
    r = client.post("/api/ledger/books", json={
        "name": "契约测试", "date_from": "2026-10-01", "date_to": "2026-10-31",
    })
    assert r.status_code == 200, r.text
    book = r.json()
    bid = book["book_id"]
    assert book["root_id"]
    assert len(book["legs"]) == 4

    # ② 列表（query）
    r = client.get("/api/ledger/books")
    assert r.status_code == 200, r.text
    assert any(b["book_id"] == bid for b in r.json()["books"])

    # ③ 整棵树（query）
    r = client.get("/api/ledger/tree", params={"book_id": bid})
    assert r.status_code == 200, r.text
    assert r.json()["root_id"] == book["root_id"]

    # ④ 状态（query）
    r = client.get("/api/ledger/status", params={"book_id": bid})
    assert r.status_code == 200, r.text
    assert r.json()["invariants_ok"] is True

    # ⑤ 写操作（body，判别联合）
    r = client.post("/api/ledger/op", json={
        "book_id": bid,
        "op": {
            "op": "create_node",
            "parent_id": book["root_id"],
            "name": "一组",
            "op_value": "ADD",
            "tx_ids": [1, 2],
        },
    })
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["root_id"] == book["root_id"]
    assert len(payload["nodes"]) == 2

    # ⑥⑦⑧ 撤销 / 重做 / 回到 M₀ —— **参数在 body 里**
    for path in ("/api/ledger/undo", "/api/ledger/redo", "/api/ledger/reset"):
        r = client.post(path, json={"book_id": bid})
        assert r.status_code == 200, f"{path} 拒绝了 body 形状：{r.text}"
        assert r.json()["root_id"] == book["root_id"]


def test_unknown_field_in_op_is_rejected(client):
    """多传一个字段就该被拒（extra="forbid"）—— 前端拼错字段要立刻红。

    注意状态码是 **400** 不是 422：main.py 的 RequestValidationError
    处理器把「试图改原始字段」翻译成 403，其余校验错误统一 400。
    """
    r = client.post("/api/ledger/books", json={
        "name": "x", "date_from": "2026-10-01", "date_to": "2026-10-31",
        "oops": 1,
    })
    assert r.status_code in (400, 422), r.text
    assert "oops" in r.text, r.text


def test_ledger_error_shape_is_stable(client):
    """引擎拒绝时的响应体必须是 {code, message, detail}。"""
    book = client.post("/api/ledger/books", json={
        "name": "错误语义", "date_from": "2026-10-01", "date_to": "2026-10-31",
    }).json()
    r = client.post("/api/ledger/op", json={
        "book_id": book["book_id"],
        "op": {"op": "split", "tx_id": 1},
    })
    assert r.status_code == 400, r.text
    body = r.json()
    assert set(body) == {"code", "message", "detail"}, body
    assert body["code"] == "SPLIT_NOT_APPLICABLE"
    assert body["message"], "拒绝必须有人话理由"
