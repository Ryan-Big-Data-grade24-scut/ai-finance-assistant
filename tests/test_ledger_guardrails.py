"""
tests/test_ledger_guardrails.py —— 结构性护栏

**这些测试不测功能，测「结构上不可达」。**

第一轮的核心结论：规则要靠**结构**保证，不能靠纪律。
所以护栏也是机器检查的 —— 有人不小心越界，CI 立刻红。

每条护栏都对应一个真实的越界可能：
    有人为了让 AI 分类插件「方便」在 MCP 工具里加了 SQL
    有人嫌麻烦在 main.py 里直接查库
    有人顺手改了 v_tx 导致 7 个报告模块连环炸
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BACKEND = ROOT / "backend"
LEDGER = BACKEND / "ledger"
FRONTEND = ROOT / "frontend" / "src"


def _py_files() -> list[Path]:
    return sorted(BACKEND.rglob("*.py"))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_text(path))


def _selftest_lines(path: Path) -> set:
    """_selftest 函数体内的行号。

    自检脚本要往**临时库**里塞测试数据 —— 那不是业务路径，
    不算「边界层含 SQL」。
    """
    lines = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_selftest":
            for sub in ast.walk(node):
                if isinstance(sub, ast.stmt):
                    lines.add(sub.lineno)
    return lines


# ──────────────────────────────────────────────────────────────
#  护栏 1：边界层不含 SQL
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", ["main.py", "mcp/server.py"])
def test_boundary_has_no_sql(rel):
    """main.py / mcp/server.py 不含任何 SQL。

    这两个文件是**边界**：它们只负责翻译。SQL 一旦出现在这里，
    就等于多了一条绕过引擎的写路径。

    唯一例外：mcp/server.py 的 `_selftest` —— 它要往临时库里塞数据。
    """
    path = BACKEND / rel
    allowed = _selftest_lines(path) if rel.endswith("server.py") else set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    assert node.lineno in allowed, f"{rel}:{node.lineno} import 了 sqlite3"
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", getattr(fn, "id", ""))
            if name in ("execute", "executemany", "executescript", "cursor"):
                assert node.lineno in allowed, \
                    f"{rel}:{node.lineno} 里有 SQL 调用（{name}）—— 边界层不许碰 SQL"


# ──────────────────────────────────────────────────────────────
#  护栏 2：账本层不 import 第一轮
# ──────────────────────────────────────────────────────────────

def test_ledger_does_not_import_first_round():
    """账本层不得 import reports / overrides / importer。

    账本是**第四条平行通道**。一旦它 import 了第一轮，
    两套口径就会在某个函数里悄悄合流。
    """
    for path in LEDGER.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
                mods.extend(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
            for mod in mods:
                head = mod.lstrip(".").split(".")[-1]
                assert head not in ("reports", "overrides", "importer"), \
                    f"{path.name} 里 import 了 {mod}"


def test_ledger_does_not_use_counted_view():
    """账本层绝不查 v_tx_counted —— 那是第一轮的口径，账本不继承。"""
    for path in LEDGER.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", "")
            if name not in ("execute", "executemany"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    assert "v_tx_counted" not in arg.value, \
                        f"{path.name} 查了 v_tx_counted —— 那是第一轮的口径"


# ──────────────────────────────────────────────────────────────
#  护栏 3：DB_PATH 的 import 方式
# ──────────────────────────────────────────────────────────────

def test_no_direct_db_path_import():
    """全仓不得 `from .db import DB_PATH`。

    直接 import 会把 DB_PATH 绑死在 import 时刻，
    测试里 monkeypatch 就改不动了。
    """
    pat = re.compile(r"from\s+[\w.]*db\s+import\s+[^\n]*\bDB_PATH\b")
    for path in _py_files():
        hit = pat.search(_text(path))
        assert not hit, f"{path} 直接 import 了 DB_PATH：{hit.group(0)}"


# ──────────────────────────────────────────────────────────────
#  护栏 4：ledger_* 表只有 store.py 能写
# ──────────────────────────────────────────────────────────────

WRITE_PAT = re.compile(
    r"(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+ledger_", re.I)


def test_only_store_writes_ledger_tables():
    """写 ledger_* 表的代码只允许出现在 backend/ledger/store.py。"""
    offenders = []
    for path in _py_files():
        if path.parent == LEDGER and path.name == "store.py":
            continue
        for m in WRITE_PAT.finditer(_text(path)):
            offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert not offenders, "写 ledger_* 表的代码跑到了别处：\n" + "\n".join(offenders)


def test_store_does_not_export_connection():
    """store.py 不导出连接 —— 拿到 store 也拿不到随手写 SQL 的能力。"""
    tree = _tree(LEDGER / "store.py")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            assert node.name not in ("get_conn", "connection")
        if isinstance(node, ast.Assign):
            for t in node.targets:
                assert not (isinstance(t, ast.Name) and t.id in ("conn", "connection"))


# ──────────────────────────────────────────────────────────────
#  护栏 5：原始层不可变
# ──────────────────────────────────────────────────────────────

def test_raw_layer_is_immutable():
    """全仓不得 UPDATE / DELETE transactions_raw。"""
    for path in _py_files():
        text = _text(path)
        assert not re.search(r"UPDATE\s+transactions_raw", text, re.I), \
            f"{path} 改了原始层"
        assert not re.search(r"DELETE\s+FROM\s+transactions_raw", text, re.I), \
            f"{path} 删了原始层"


def test_v_tx_view_not_altered():
    """v_tx / v_tx_counted 的定义不许被改。

    7 个报告模块全 FROM v_tx —— 改它就是连环炸。
    （本轮账本层因此改成 JOIN 原始层补 merchant_no，而不是改视图。）
    """
    schema = _text(ROOT / "db" / "schema.sql")
    assert "CREATE VIEW v_tx AS" in schema
    assert "CREATE VIEW IF NOT EXISTS v_tx_counted AS" in schema
    for col in ("direction", "amount", "category", "is_exempt", "funding", "month"):
        assert col in schema


# ──────────────────────────────────────────────────────────────
#  护栏 6：账本层的 print
# ──────────────────────────────────────────────────────────────

def test_ledger_has_no_print():
    """backend/ledger/ 里不许 print。

    stdout 在 MCP 的 stdio 模式是**协议通道**，一个 print 就能让会话崩掉。
    """
    for path in LEDGER.rglob("*.py"):
        assert "print(" not in _text(path), f"{path.name} 里有 print"


def test_mcp_print_only_in_selftest():
    """mcp/server.py 的 print 只允许出现在 _selftest 里。"""
    path = BACKEND / "mcp" / "server.py"
    allowed = _selftest_lines(path)
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "print":
            assert node.lineno in allowed, \
                f"mcp/server.py:{node.lineno} 有 print —— stdout 是协议通道"


# ──────────────────────────────────────────────────────────────
#  护栏 7：MCP 工具 schema 不泄漏原始字段
# ──────────────────────────────────────────────────────────────

def test_ledger_tool_schema_has_no_raw_fields():
    """账本工具的入参里不许出现原始字段名 / 表名。

    这是「AI 分类插件改不了原始数据」的协议层防线。
    """
    import asyncio

    from backend.mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    banned = {"amount", "tx_time", "direction", "counterparty", "pay_method",
              "raw_json", "merchant_no", "ledger_nodes", "ledger_legs",
              "transactions_raw", "tx_overrides"}
    for tool in tools:
        if not tool.name.startswith(("query_ledger", "apply_ledger")):
            continue
        keys = set()

        def walk(node):
            if isinstance(node, dict):
                for name, sub in (node.get("properties") or {}).items():
                    keys.add(name)
                    walk(sub)
                for sub in (node.get("$defs") or {}).values():
                    walk(sub)

        walk(tool.inputSchema or {})
        leaked = keys & banned
        assert not leaked, f"{tool.name} 的 schema 泄漏了 {sorted(leaked)}"


# ──────────────────────────────────────────────────────────────
#  护栏 8：前后端参数名一致
# ──────────────────────────────────────────────────────────────

def test_frontend_route_params_match_backend():
    """前端路由的 query 参数名必须和后端一致。

    前端 query 参数写错 ⇒ 参数被静默忽略 ⇒ 前端以为聚焦了节点，
    后端其实返回了整棵树。这类 bug 极难发现，所以机器查。
    """
    router = _text(FRONTEND / "router" / "index.ts")
    for name in ("book_id", "focus_node_id"):
        assert f"route.query.{name}" in router, \
            f"前端路由里没有 {name} —— 参数名必须和后端一致"
    # 建账表单用 date_from / date_to —— 和后端 LedgerBookCreate 对齐
    picker = _text(FRONTEND / "components" / "ledger" / "BookPicker.vue")
    api = _text(FRONTEND / "api" / "ledger.ts")
    for name in ("date_from", "date_to"):
        assert name in api, f"前端 API 里没有 {name} —— 参数名必须和后端一致"
    assert "dateFrom" in picker and "dateTo" in picker


# ──────────────────────────────────────────────────────────────
#  护栏 10：前端发的参数，后端必须真的收得到
# ──────────────────────────────────────────────────────────────

def test_frontend_ledger_payload_matches_backend_body():
    """前端 postJson 的 payload 键，必须是后端 body 模型的字段。

    **为什么需要这条**：第一版骨架里，前端把 `book_id` 塞进 body、
    后端按 query 收 —— 结果「撤销 / 重做 / 回到 M₀」三个按钮全坏，
    而且**两边都不报错**：后端 422，前端只弹「请求失败」。
    单元测试全绿，之前的护栏也全绿 —— 因为它们只查了参数**名**，
    没查参数**放在哪**。

    这条护栏查两件事：
        ① 前端 payload 的每个键，后端 body 模型里得有（拼错字段要红）
        ② 后端的账本 POST 路由不许有「必填的 query 参数」
    """
    import re

    from backend import main as main_mod

    # ① 前端：抓 postJson('/api/ledger/xxx', { ... }) 的路径和键
    api = _text(FRONTEND / "api" / "ledger.ts")
    calls = re.findall(
        r"postJson<[^>]*>\(\s*'(/api/ledger[^']*)'\s*,\s*\{([^}]*)\}", api)
    assert calls, "没在 api/ledger.ts 里抓到 postJson 调用 —— 护栏本身失效了"

    # ② 后端：POST 路由 → body 模型 → 字段集合
    body_fields: dict[str, set[str]] = {}
    for route in main_mod.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/ledger") or "POST" not in methods:
            continue
        body_params = getattr(route.dependant, "body_params", [])
        assert body_params, f"{path} 的 POST handler 没有 body 模型"
        model = body_params[0].field_info.annotation
        assert hasattr(model, "model_fields"), f"{path} 的 body 不是 Pydantic 模型：{model}"
        body_fields[path] = set(model.model_fields)

    problems = []
    for route, payload in calls:
        keys = set()
        for part in payload.split(","):
            part = part.strip()
            if not part:
                continue
            key = part.split(":")[0].strip()
            if key:
                keys.add(key)
        if route not in body_fields:
            problems.append(f"{route}：后端没有这个 POST 路由")
            continue
        extra = keys - body_fields[route]
        if extra:
            problems.append(
                f"{route}：前端发了后端 body 里没有的字段 {sorted(extra)}"
                f"（后端收的是 {sorted(body_fields[route])}）")
    assert not problems, "\n".join(problems)


def test_ledger_post_routes_have_no_required_query_params():
    """账本的 POST 路由不许有必填的 query 参数 —— POST 的参数一律走 body。

    混着来过一次（body vs query），三个按钮静默坏掉。
    """
    from backend import main as main_mod

    offenders = []
    for route in main_mod.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/ledger") or "POST" not in methods:
            continue
        for q in getattr(route.dependant, "query_params", []):
            if q.required:
                offenders.append(f"{path} 的 query 参数 {q.name} 是必填的")
    assert not offenders, "\n".join(offenders)


# ──────────────────────────────────────────────────────────────
#  护栏 9：依赖方向单向无环
# ──────────────────────────────────────────────────────────────

def test_ledger_imports_only_allowed_modules():
    """backend/ledger/* 只允许 import backend.db 和 backend.schemas。"""
    for path in LEDGER.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lstrip(".")
                # node.level=1 是包内相对导入（from .amount import ...）—— 允许
                # node.level>=2 才跨出账本包
                if node.level >= 2 or mod.startswith("backend"):
                    assert mod in ("db", "schemas", "backend.db", "backend.schemas"), \
                        f"{path.name} 里 import 了 {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name.startswith("backend"):
                        assert name in ("backend.db",), \
                            f"{path.name} 里 import 了 {name}"
