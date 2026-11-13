"""
tests/test_ledger_http_contract.py —— V3 HTTP 层的**契约**测试

**为什么单独有这一份**：
    第一版骨架里，前端把 `book_id` 塞进 body、后端按 query 收 ——
    结果「撤销 / 重做 / 回到 M₀」三个按钮**全是坏的**：
    后端 422，前端只显示一句「请求失败」，两边都不报错。
    单元测试全绿（引擎没问题），护栏也全绿（护栏只查了参数**名**，
    没查参数**放在哪**）。

    所以这里补三件事：
      ① 用 TestClient 把 V3 的账本端点**按前端 api/ledger.ts 的原样**调一遍
         —— 形状不对就红。
      ② 响应体的字段集合，按**前端 TS 接口的必填字段**核对
         （`frontend/src/api/ledger.ts::LedgerTree` 等）——
         后端少给一个字段，前端 TS 编译器不会报错，运行时才炸。
      ③ 错误体必须是 `{code, message, detail}` 三键；多传一个字段要立刻被拒，
         而且错误里必须能看出**是哪个字段**错了。

**V3 的两处变化**（相对第一轮骨架）：
    · 账本 id 在 `tree["book"]["id"]`，不再是 `tree["book_id"]`。
    · 多了一个 `/api/ledger/live`（自动同步账本），前端 `getLiveLedgerTree`
      用的就是它；`/api/ledger/status` 前端暂时没调，但它是文档里的第 8 个端点，
      这里一并核对。

⚠️ 这一份不测业务，只测**接口形状**。业务在 test_ledger_engine_v3.py 等。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod                # noqa: E402

API_TS = ROOT / "frontend" / "src" / "api" / "ledger.ts"


# ──────────────────────────────────────────────────────────────
#  前端 TS 接口 → 必填字段集合
# ──────────────────────────────────────────────────────────────

def _ts_interface_required_fields(src: str, name: str) -> set[str]:
    """从 `export interface X { ... }` 里解出**必填**字段名（带 `?` 的不算）。

    前端 TS 接口是「后端该返回什么」的真相源：可选字段少一个不炸，
    必填字段少一个，前端运行时直接 undefined 崩。
    """
    m = re.search(rf"export interface {re.escape(name)}\s*\{{(.*?)\n\}}", src, re.S)
    assert m, f"前端 api/ledger.ts 里找不到 interface {name} —— 契约测试本身失效了"
    body = m.group(1)
    required = set()
    for field, optional in re.findall(r"(\w+)(\?)?\s*:", body):
        if not optional:
            required.add(field)
    assert required, f"interface {name} 一个必填字段都没解析出来 —— 契约测试本身失效了"
    return required


def _assert_shape(payload: dict, required: set[str], where: str) -> None:
    missing = required - set(payload)
    assert not missing, f"{where} 缺少前端必填字段：{sorted(missing)}"


# ──────────────────────────────────────────────────────────────
#  测试装置
# ──────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 几笔合成交易 + TestClient。

    ⚠️ 绝对不碰 `db/bills.db`（真实用户库）：DB_PATH 被 monkeypatch 到
    tmp_path，app 启动时的 `db.init_db()` 建的是临时库。
    """
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

    # 用上下文管理器：启动事件要跑 `db.init_db()` + `sync_live_book()`，
    # 否则 /api/ledger/live 没有自动同步账本可读。
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def frontend_src() -> str:
    return API_TS.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────
#  ① 所有账本端点按前端的写法调一遍
# ──────────────────────────────────────────────────────────────

def test_all_ledger_endpoints_answer_with_frontend_shapes(client, frontend_src):
    """V3 的账本端点全部按**前端 api/ledger.ts 的写法**调一遍。

    调用顺序照抄前端真实用法：
        建账 → 列表 → 树 → 状态 → 写操作 → 撤销 → 重做 → 回到 M₀ → live 视图
    每一步的响应体都按前端 TS 接口的**必填字段**核对。
    """
    tree_required = _ts_interface_required_fields(frontend_src, "LedgerTree")
    book_required = _ts_interface_required_fields(frontend_src, "LedgerBook")
    node_required = _ts_interface_required_fields(frontend_src, "LedgerNode")
    tx_required = _ts_interface_required_fields(frontend_src, "LedgerTx")
    leg_required = _ts_interface_required_fields(frontend_src, "LedgerLeg")

    def check_tree(payload: dict, where: str) -> dict:
        _assert_shape(payload, tree_required, where)
        _assert_shape(payload["book"], book_required, f"{where}.book")
        for node in payload["nodes"]:
            _assert_shape(node, node_required, f"{where}.nodes[]")
        for tx in payload["transactions"]:
            _assert_shape(tx, tx_required, f"{where}.transactions[]")
        for leg in payload.get("legs", []):
            _assert_shape(leg, leg_required, f"{where}.legs[]")
        return payload

    # ① 建账（body）：前端的 createLedgerBook(name, date_from, date_to)
    r = client.post("/api/ledger/books", json={
        "name": "契约测试", "date_from": "2026-10-01", "date_to": "2026-10-31",
    })
    assert r.status_code == 200, r.text
    created = check_tree(r.json(), "POST /api/ledger/books")
    bid = created["book"]["id"]           # V3：账本 id 在 book.id
    assert created["root_id"]
    assert len(created["legs"]) == 4

    # ② 列表（query）：前端的 listLedgerBooks()
    r = client.get("/api/ledger/books")
    assert r.status_code == 200, r.text
    listing = r.json()
    assert "books" in listing
    assert any(b["id"] == bid for b in listing["books"]), listing
    for b in listing["books"]:
        _assert_shape(b, book_required, "GET /api/ledger/books[].books[]")

    # ③ 整棵树（query）：前端的 getLedgerTree(book_id)
    r = client.get("/api/ledger/tree", params={"book_id": bid})
    assert r.status_code == 200, r.text
    tree = check_tree(r.json(), "GET /api/ledger/tree")
    assert tree["root_id"] == created["root_id"]

    # ④ 状态（query）：第 8 个端点，前端暂未调用，但文档里有它
    r = client.get("/api/ledger/status", params={"book_id": bid})
    assert r.status_code == 200, r.text
    status = r.json()
    for key in ("book_id", "total_tx", "placed_tx", "invariants_ok", "invariants",
                "can_undo", "can_redo", "current_seq", "leg_count", "warnings",
                "audit_ok", "split_count", "audit"):
        assert key in status, f"GET /api/ledger/status 缺字段 {key}"
    assert status["invariants_ok"] is True
    assert status["book_id"] == bid

    # ⑤ 写操作（body，判别联合）：前端的 applyLedgerOp(book_id, op, expected_seq)
    r = client.post("/api/ledger/op", json={
        "book_id": bid,
        "op": {
            "op": "create_node",
            "parent_id": tree["root_id"],
            "name": "一组",
            "op_value": "ADD",
            "tx_ids": [1, 2],
        },
        "expected_seq": tree["seq"],
    })
    assert r.status_code == 200, r.text
    after_op = check_tree(r.json(), "POST /api/ledger/op")
    assert after_op["root_id"] == tree["root_id"]
    assert len(after_op["nodes"]) == len(tree["nodes"]) + 1
    assert after_op["seq"] == tree["seq"] + 1

    # ⑥⑦⑧ 撤销 / 重做 / 回到 M₀ —— **参数在 body 里**（前端的 historyLedger）
    seq = after_op["seq"]
    r = client.post("/api/ledger/undo", json={"book_id": bid, "expected_seq": seq})
    assert r.status_code == 200, f"/api/ledger/undo 拒绝了 body 形状：{r.text}"
    undone = check_tree(r.json(), "POST /api/ledger/undo")
    assert undone["seq"] == seq - 1
    assert undone["can_redo"] is True

    r = client.post("/api/ledger/redo", json={"book_id": bid, "expected_seq": undone["seq"]})
    assert r.status_code == 200, f"/api/ledger/redo 拒绝了 body 形状：{r.text}"
    redone = check_tree(r.json(), "POST /api/ledger/redo")
    assert redone["seq"] == undone["seq"] + 1

    r = client.post("/api/ledger/reset", json={"book_id": bid, "expected_seq": redone["seq"]})
    assert r.status_code == 200, f"/api/ledger/reset 拒绝了 body 形状：{r.text}"
    reset = check_tree(r.json(), "POST /api/ledger/reset")
    assert reset["root_id"] == tree["root_id"]

    # ⑨ live 视图：前端的 getLiveLedgerTree(filters)
    r = client.get("/api/ledger/live")
    assert r.status_code == 200, r.text
    live = check_tree(r.json(), "GET /api/ledger/live")
    for key in ("source_count", "total_count", "database_path", "filters"):
        assert key in live, f"GET /api/ledger/live 缺字段 {key}"
    assert live["filters"] == {"date_from": None, "date_to": None, "category_node_id": None}

    # 带过滤条件（前端 StarFilters 三个字段都能传）
    r = client.get("/api/ledger/live", params={
        "date_from": "2026-10-01", "date_to": "2026-10-31",
    })
    assert r.status_code == 200, r.text
    filtered = check_tree(r.json(), "GET /api/ledger/live?date_from&date_to")
    assert filtered["filters"]["date_from"] == "2026-10-01"
    assert filtered["count"] == 4
    assert filtered["total_count"] == 4

    # category_node_id 也得能被前端原样传进来
    node_id = filtered["nodes"][0]["id"]
    r = client.get("/api/ledger/live", params={"category_node_id": node_id})
    assert r.status_code == 200, r.text
    assert r.json()["filters"]["category_node_id"] == node_id


# ──────────────────────────────────────────────────────────────
#  ② 多传一个字段 → 立刻被拒，且错误里说得清是哪个字段
# ──────────────────────────────────────────────────────────────

def test_unknown_field_in_op_is_rejected(client):
    """op 里多传一个字段就该被拒（extra="forbid"）—— 前端拼错字段要立刻红。

    注意状态码是 **400** 不是 422：main.py 的 RequestValidationError
    处理器把校验错误统一翻成 400（只有「试图改原始字段」才是 403）。

    而且错误体里必须**看得出是哪个字段错了**：`loc` 形如
    `["body", "op", "set_op", "oops"]`，末尾就是那个多余的字段名。
    """
    book = client.post("/api/ledger/books", json={
        "name": "契约测试", "date_from": "2026-10-01", "date_to": "2026-10-31",
    }).json()
    bid = book["book"]["id"]

    r = client.post("/api/ledger/op", json={
        "book_id": bid,
        "op": {"op": "set_op", "node_id": book["root_id"], "op_value": "ADD", "oops": 1},
    })
    assert r.status_code in (400, 422), r.text
    body = r.json()
    assert "detail" in body, body
    locs = [list(err.get("loc", ())) for err in body["detail"]]
    assert any(loc[-1] == "oops" for loc in locs), f"错误里没有指出多余的字段名：{body}"
    # 字段名要出现在整段错误文本里，方便前端直接把原因弹给用户
    assert "oops" in r.text, r.text


# ──────────────────────────────────────────────────────────────
#  ③ 引擎拒绝时的响应体形状
# ──────────────────────────────────────────────────────────────

def test_ledger_error_shape_is_stable(client):
    """引擎拒绝时的响应体必须是 `{code, message, detail}` —— 一个键都不能多、不能少。

    `code` 必须是 `backend/ledger/errors.py::Code` 里的常量（不许写字面量）；
    `message` 必须是**人话**（前端要直接弹给用户，MCP 要逐笔说明理由）；
    `detail` 必须是对象（以后要放上下文时不用改契约）。

    这里只约束**引擎自己产生的错误**（`LedgerError`）。
    请求体本身不合法（字段拼错、类型不对）是 FastAPI 在进引擎之前
    就拦下的，那条路走的是 `RequestValidationError` 处理器，
    响应体是 `{"detail": [...]}` —— 那是 HTTP 层的形状，不是账本的形状。
    """
    from backend.ledger.errors import Code

    known_codes = {
        value for name, value in vars(Code).items()
        if not name.startswith("_") and isinstance(value, str)
    }

    book = client.post("/api/ledger/books", json={
        "name": "错误语义", "date_from": "2026-10-01", "date_to": "2026-10-31",
    }).json()
    bid = book["book"]["id"]

    # ① 引擎语义错误：没配外部额度的交易不能分裂
    r = client.post("/api/ledger/op", json={
        "book_id": bid,
        "op": {"op": "split", "tx_id": 1},
    })
    assert r.status_code == 400, r.text
    body = r.json()
    assert set(body) == {"code", "message", "detail"}, body
    assert body["code"] == "SPLIT_NOT_APPLICABLE"
    assert body["code"] in known_codes, f"错误码没有登记在 errors.py::Code：{body['code']}"
    assert body["message"], "拒绝必须有人话理由"
    assert isinstance(body["detail"], dict), body

    # ② 查不到账本：404，三键形状不变
    r = client.get("/api/ledger/tree", params={"book_id": "no-such-book"})
    assert r.status_code == 404, r.text
    body = r.json()
    assert set(body) == {"code", "message", "detail"}, body
    assert body["code"] == "BOOK_NOT_FOUND"
    assert body["code"] in known_codes

    # ③ 乐观锁冲突：expected_seq 对不上 → 409（前端要能区分「该刷新了」）
    r = client.post("/api/ledger/op", json={
        "book_id": bid,
        "op": {"op": "set_op", "node_id": book["root_id"], "op_value": "NET"},
        "expected_seq": 999,
    })
    assert r.status_code == 409, r.text
    body = r.json()
    assert set(body) == {"code", "message", "detail"}, body
    assert body["code"] == "STALE_REVISION"
