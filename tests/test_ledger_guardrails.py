"""
tests/test_ledger_guardrails.py —— 结构性护栏（V3 账本引擎）

**这些测试不测功能，测「结构上不可达」。**

第一轮的核心结论：规则要靠**结构**保证，不能靠纪律。
所以护栏也是机器检查的 —— 有人不小心越界，CI 立刻红。

每条护栏都对应一个真实的越界可能：
    有人为了让 AI 分类插件「方便」在 MCP 工具里加了 SQL
    有人嫌麻烦在 main.py 里直接查库
    有人顺手改了 v_tx 导致 7 个报告模块连环炸

V3 变化（相对 V2 护栏模板）：
    · `backend/ledger/tree.py` 已删除 —— 树的聚合改由 atoms/invariants 承担
    · 新增 `backend/ledger/migrate.py` —— 一次性迁移脚本
    · 账本表换成 ledger_books / ledger_book_tx / ledger_nodes /
      ledger_legs / ledger_history / ledger_placements（旧的
      ledger_ops / ledger_book_state / ledger_snapshots / ledger_seq 已退役）
    · 前端从 `components/ledger/*` + `stores/ledger.ts` 换成
      `views/StarView.vue` + `stars/useStarGraph.ts` + `api/ledger.ts`

护栏的**语义一条都没丢**，只是按 V3 的实际模块结构改写路径与断言。
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
API_TS = FRONTEND / "api" / "ledger.ts"

# 账本层允许碰的后端模块（V3 的纪律：只许读 db / schemas，其余一概不许）。
ALLOWED_BACKEND_MODULES = {"backend.db", "backend.schemas"}


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


def _import_targets(node: ast.AST) -> list[str]:
    """把 import 语句解析成它**实际指向的模块名**。

    A 树的解析器只认 `node.module`，于是 `from .. import db`（module 是 None）
    被算成空字符串 → 误判成「越界」。这一版必须把相对导入的
    真实目标算出来：

        from .. import db          → backend.db
        from ..db import get_conn  → backend.db
        from . import loader       → loader（包内 backend.ledger.loader）
        from .amount import cents  → amount（包内）
        import backend.db          → backend.db

    包内相对导入只返回**叶子名**（loader / amount / …），
    这样「不 import 第一轮」那条护栏仍能认出 `from . import reports`，
    而「只许 import backend.db / backend.schemas」那条也不会误伤包内导入。
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level == 0:
        base = node.module or ""
        if base == "backend":
            # from backend import db / schemas
            return [f"backend.{alias.name}" for alias in node.names]
        return [base]
    if node.level == 1:
        # 包内相对导入（. / .amount）：叶子名，天然在 backend.ledger 里
        base = node.module
        if base:
            return [base]
        return [alias.name for alias in node.names]
    # level >= 2：从 backend.ledger 跨到 backend
    base = node.module
    if base:
        return [f"backend.{base}"]
    # from .. import db  /  from .. import schemas
    return [f"backend.{alias.name}" for alias in node.names]


# ──────────────────────────────────────────────────────────────
#  护栏 1：边界层不含 SQL
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel", ["main.py", "mcp/server.py"])
def test_boundary_has_no_sql(rel):
    """main.py / mcp/server.py 不含任何 SQL。

    这两个文件是**边界**：它们只负责翻译。SQL 一旦出现在这里，
    就等于多了一条绕过引擎的写路径 —— 账本口径和第一轮口径
    会在同一个函数里悄悄合流。

    唯一例外：mcp/server.py 的 `_selftest` —— 它要往临时库里塞数据。

    ⚠️ V3 现状：**main.py 是红的**（见 tests/护栏覆盖说明.md）。
    越界的是 health / import_history / ai classification 三处直查。
    """
    path = BACKEND / rel
    allowed = _selftest_lines(path) if rel.endswith("server.py") else set()
    offenders = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3" and node.lineno not in allowed:
                    offenders.append(f"{rel}:{node.lineno} import 了 sqlite3")
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", getattr(fn, "id", ""))
            if name in ("execute", "executemany", "executescript", "cursor"):
                if node.lineno not in allowed:
                    offenders.append(
                        f"{rel}:{node.lineno} 里有 SQL 调用（{name}）—— 边界层不许碰 SQL")
    assert not offenders, "边界层出现 SQL：\n" + "\n".join(offenders)


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
            for target in _import_targets(node):
                head = target.lstrip(".").split(".")[-1]
                assert head not in ("reports", "overrides", "importer"), \
                    f"{path.name} 里 import 了 {target}"


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
    """写 ledger_* 表的代码只允许出现在 backend/ledger/store.py。

    V3 的表集是 ledger_books / ledger_book_tx / ledger_nodes /
    ledger_legs / ledger_history / ledger_placements —— 正则一次盖全，
    新表将来加进来也不用改护栏。

    写成正则匹配整份源码，所以**注释里写一句示例 SQL 也会红** ——
    这是故意的：护栏要的是「文件里根本没有这些字样」。
    """
    offenders = []
    for path in _py_files():
        if path.parent == LEDGER and path.name == "store.py":
            continue
        for m in WRITE_PAT.finditer(_text(path)):
            offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert not offenders, "写 ledger_* 表的代码跑到了别处：\n" + "\n".join(offenders)


def test_store_does_not_export_connection():
    """store.py 不导出连接 —— 拿到 store 也拿不到随手写 SQL 的能力。

    store.py 的函数**全部**以 `conn` 为入参：连接由 service 层开、由
    service 层关，store 只负责在已开的事务里执行语句。
    """
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

    V3 允许 v_tx **新增列**（分类优先级引入了 ledger 侧投影），
    但视图本身必须还在、且核心列一个都不能少 —— 少一列，
    7 个报告模块里就会有一个报 KeyError。
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

    ⚠️ V3 现状：**这条是红的**。越界点是 backend/ledger/migrate.py 的
    `main()`（迁移脚本把结果 print 出来）。
    修法：改用 `logging` / `sys.stderr.write`，或者把脚本挪出
    backend/ledger/（比如挪到 scripts/）。
    """
    offenders = []
    for path in LEDGER.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "print":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} 有 print")
    assert not offenders, "账本层出现 print（stdout 是 MCP 协议通道）：\n" + \
        "\n".join(offenders)


def test_mcp_print_only_in_selftest():
    """mcp/server.py 的 print 只允许出现在 _selftest 里。"""
    path = BACKEND / "mcp" / "server.py"
    allowed = _selftest_lines(path)
    offenders = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "print":
            if node.lineno not in allowed:
                offenders.append(f"mcp/server.py:{node.lineno} 有 print")
    assert not offenders, "\n".join(offenders)


# ──────────────────────────────────────────────────────────────
#  护栏 7：MCP 工具 schema 不泄漏原始字段
# ──────────────────────────────────────────────────────────────

def test_ledger_tool_schema_has_no_raw_fields():
    """账本工具的入参里不许出现原始字段名 / 表名。

    这是「AI 分类插件改不了原始数据」的协议层防线。
    V3 的三个账本工具：query_ledger_books / query_ledger_tree /
    apply_ledger_op —— 最后一个的参数是**扁平可选参数**，
    正好是最容易顺手加一个 `amount` 的地方，所以更要盯死。
    """
    import asyncio

    from backend.mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    banned = {"amount", "tx_time", "direction", "counterparty", "pay_method",
              "raw_json", "merchant_no", "ledger_nodes", "ledger_legs",
              "transactions_raw", "tx_overrides", "tx_overrides"}
    seen = 0
    for tool in tools:
        if not tool.name.startswith(("query_ledger", "apply_ledger")):
            continue
        seen += 1
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
    assert seen >= 3, f"账本 MCP 工具只剩 {seen} 个 —— 护栏本身失效了"


# ──────────────────────────────────────────────────────────────
#  护栏 8：前后端 query 参数名一致
# ──────────────────────────────────────────────────────────────

# 前端 api/ledger.ts 里 `request(...)` 的调用形态：
#   request<{books: LedgerBook[]}>('/api/ledger/books')          ← GET
#   request<LedgerTree>('/api/ledger/books', { name, date_from }) ← POST
#   request<LedgerTree>(`/api/ledger/tree?book_id=${...}`)        ← GET（模板串）
#   request<LedgerTree>(`/api/ledger/${action}`, { book_id })     ← POST（模板串）
_FRONTEND_CALL = re.compile(
    r"request<[^>]*>\(\s*(?:'([^']*)'|`([^`]*)`)\s*([,)])")
# getLiveLedgerTree 用 URLSearchParams 拼查询串
_FRONTEND_QSET = re.compile(r"q\.set\(\s*'([A-Za-z_]\w*)'")
# 模板串里的插值 `${expr}`
_TEMPLATE = re.compile(r"\$\{([^}]*)\}")


def _ts_union_values(src: str, var: str) -> set[str]:
    """从一个 TS 形参上解出它的字面量联合类型。

    例如 `action: 'undo' | 'redo' | 'reset'` → {'undo','redo','reset'}。
    用来把前端模板串 `/api/ledger/${action}` 展开成后端真实存在的
    undo / redo / reset 三条路由 —— 否则护栏无法判断 payload 形状。
    """
    m = re.search(rf"\b{re.escape(var)}\s*:\s*([^,)=;\n]+)", src)
    if not m:
        return set()
    return set(re.findall(r"'([^']*)'", m.group(1))) - {""}


class FrontendCall:
    """一次前端后端调用。

    V3 前端的 `api/ledger.ts` 用一个自己的 `request<T>()` 包 fetch，
    调用形态有四种：字面量 URL、带 body 的字面量 URL、
    带插值的模板串 URL、带插值 + body 的模板串 URL。
    这一类把四种统一解析成「路径集合 + query 参数 + body 键」。
    """

    def __init__(self, raw: str, has_body: bool, src: str):
        self.raw = raw
        self.src = src
        self.has_body = has_body
        self.method = "POST" if has_body else "GET"
        self.body_keys: set[str] = set()
        # 先把 `${expr}` 挖掉，剩下的才是「字面量部分」
        stripped = _TEMPLATE.sub("\x00", raw)
        literal, _, query = stripped.partition("?")
        self.query_literal = query
        self.base_prefix = literal.split("\x00")[0]
        # 动态段：只有「简单标识符 + 能解出字面量联合类型」才能展开成多条路径。
        # 展开不了（比如 `${encodeURIComponent(book_id)}`）就退化成字面量前缀。
        self.paths = [self.base_prefix]
        for expr in _TEMPLATE.findall(raw):
            expr = expr.strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", expr):
                self.paths = []
                break
            values = _ts_union_values(src, expr)
            if not values:
                self.paths = []
                break
            self.paths = [p.rstrip("/") + "/" + v for p in self.paths for v in values]
        if not self.paths:
            self.paths = [self.base_prefix]

    @property
    def query_params(self) -> set[str]:
        names = set()
        for part in self.query_literal.split("&"):
            key = part.split("=")[0].strip()
            if key and "\x00" not in key:
                names.add(key)
        if self.base_prefix.endswith("/live"):
            # getLiveLedgerTree 用 URLSearchParams 拼过滤条件
            names |= set(_FRONTEND_QSET.findall(self.src))
        return names


def _frontend_calls(src: str) -> list["FrontendCall"]:
    """解析前端 api/ledger.ts 的调用。"""
    calls = []
    for m in _FRONTEND_CALL.finditer(src):
        literal = m.group(1) if m.group(1) is not None else m.group(2)
        call = FrontendCall(literal, m.group(3) == ",", src)
        if call.has_body:
            call.body_keys = _extract_body_keys(src[m.end():])
        calls.append(call)
    return calls


def _extract_body_keys(tail: str) -> set[str]:
    """从 `, { ... })` 的尾巴里抓出 body 的键集合。

    前端 V3 用简写 `{ name, date_from, date_to }`（没有 `key:`），
    所以键名要按「逗号分隔后取冒号前的部分」来解析。
    """
    brace = tail.find("{")
    if brace < 0:
        return set()
    depth = 0
    end = brace
    for i in range(brace, len(tail)):
        if tail[i] == "{":
            depth += 1
        elif tail[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = tail[brace + 1:end]
    keys = set()
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        key = part.split(":")[0].strip()
        if key:
            keys.add(key)
    return keys


def _query_is_required(param) -> bool:
    """FastAPI 各版本的 query 参数字段不一样：`required` 属性在 0.143 已移除。

    统一按「默认值是 PydanticUndefined 即必填」判断。
    注意 `str | None = None` 的默认值就是 None，那不是「没默认值」，不能算必填。
    """
    from pydantic_core import PydanticUndefined

    field_info = getattr(param, "field_info", None)
    if field_info is None:
        return bool(getattr(param, "required", False))
    default = getattr(field_info, "default", PydanticUndefined)
    return default is PydanticUndefined


def _backend_routes(method: str, path: str) -> list[tuple[str, list]]:
    """后端账本路由里「路径 + 方法」完全等于 path 的那些（含 query 参数）。"""
    from backend import main as main_mod

    routes = []
    for route in main_mod.app.routes:
        rpath = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not rpath.startswith("/api/ledger") or method not in methods:
            continue
        if rpath != path:
            continue
        routes.append((rpath, list(getattr(route.dependant, "query_params", []))))
    return routes


def test_frontend_route_params_match_backend():
    """前端发给后端的 query 参数名，必须是后端路由真正认的。

    前端 query 参数写错 ⇒ 参数被静默忽略 ⇒ 前端以为聚焦了节点，
    后端其实返回了整棵树。这类 bug 极难发现，所以机器查。

    V3 变化：前端不再用路由 query 传 book_id（`/star` 路由没有账本参数），
    账本视图的过滤条件走 `GET /api/ledger/live` 的
    date_from / date_to / category_node_id，树的读取走
    `GET /api/ledger/tree?book_id=`。所以这条护栏改成
    「前端发的 query 参数 ⊆ 后端认的 query 参数」双向核对。
    """
    src = _text(API_TS)
    calls = _frontend_calls(src)
    assert calls, "没在 api/ledger.ts 里抓到调用 —— 护栏本身失效了"

    problems = []
    checked = 0
    for call in calls:
        if call.method != "GET":
            continue
        for path in call.paths:
            backend = _backend_routes("GET", path)
            if not backend:
                problems.append(f"前端调用了后端不存在的 GET 路由：{path}")
                continue
            checked += 1
            sent = call.query_params
            for backend_path, params in backend:
                accepted = {p.name for p in params}
                required = {p.name for p in params if _query_is_required(p)}
                extra = sent - accepted
                if extra:
                    problems.append(
                        f"{backend_path}：前端发了后端不认的 query 参数 {sorted(extra)}"
                        f"（后端认的是 {sorted(accepted)}）")
                missing = required - sent
                if missing:
                    problems.append(
                        f"{backend_path}：后端必填的 query 参数 {sorted(missing)} 前端没发")
    assert checked, "一条 GET 调用都没核对上 —— 护栏本身失效了"
    assert not problems, "\n".join(problems)


# ──────────────────────────────────────────────────────────────
#  护栏 9：前端发的 body，后端必须真的收得到
# ──────────────────────────────────────────────────────────────

def test_frontend_ledger_payload_matches_backend_body():
    """前端 payload 的键，必须是后端 body 模型的字段。

    **为什么需要这条**：第一版骨架里，前端把 `book_id` 塞进 body、
    后端按 query 收 —— 结果「撤销 / 重做 / 回到 M₀」三个按钮全坏，
    而且**两边都不报错**：后端 422，前端只弹「请求失败」。
    单元测试全绿，之前的护栏也全绿 —— 因为它们只查了参数**名**，
    没查参数**放在哪**。

    这条护栏查：前端 payload 的每个键，后端 body 模型里得有（拼错字段要红）。

    V3 的前端用 `request<T>(url, body)` 而不是 `postJson`，
    但形状约束一模一样，所以解析器按 request 写。
    模板串 URL（`/api/ledger/${action}`）会被展开成
    undo / redo / reset 三条真实路由分别核对。
    """
    from backend import main as main_mod

    src = _text(API_TS)

    # ① 前端：抓 request('/api/ledger/xxx', { ... }) 的路径和键
    calls = [c for c in _frontend_calls(src) if c.method == "POST"]
    assert calls, "没在 api/ledger.ts 里抓到 POST 调用 —— 护栏本身失效了"

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
    matched_any = False
    for call in calls:
        for path in call.paths:
            if path not in body_fields:
                problems.append(f"前端调用了后端不存在的 POST 路由：{path}")
                continue
            matched_any = True
            extra = call.body_keys - body_fields[path]
            if extra:
                problems.append(
                    f"{path}：前端发了后端 body 里没有的字段 {sorted(extra)}"
                    f"（后端收的是 {sorted(body_fields[path])}）")
    assert matched_any, "一条 POST 调用都没核对上 —— 护栏本身失效了"
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
            if _query_is_required(q):
                offenders.append(f"{path} 的 query 参数 {q.name} 是必填的")
    assert not offenders, "\n".join(offenders)


# ──────────────────────────────────────────────────────────────
#  护栏 10：依赖方向单向无环
# ──────────────────────────────────────────────────────────────

def test_ledger_imports_only_allowed_modules():
    """backend/ledger/* 只允许 import backend.db 和 backend.schemas。

    其余（decimal / json / uuid4 / dataclasses …）都是标准库，随你用。
    包内相对导入（`from . import loader`）也算允许 —— 那是账本包**自己**
    在分层，不算跨层。

    V3 的 service.py 用的是 `from .. import db`（相对导入 backend 包），
    A 树的解析器把 `node.module=None` 当成空模块名 → 假阳性。
    这一版用 `_import_targets` 解析出真实目标 `backend.db`。
    """
    for path in LEDGER.rglob("*.py"):
        for node in ast.walk(_tree(path)):
            for target in _import_targets(node):
                if target.startswith("backend"):
                    assert target in ALLOWED_BACKEND_MODULES, \
                        f"{path.name} 里 import 了 {target}"


# ──────────────────────────────────────────────────────────────
#  护栏 11：V3 的账本包结构不被悄悄改回去
# ──────────────────────────────────────────────────────────────

def test_ledger_package_layout_is_v3():
    """账本包的分层文件清单必须和 V3 一致。

    理由：`tree.py` 在 V2 里既是聚合器又是数据结构的定义处，
    曾经有人往里面塞 IO（于是有了「账本层不 import 第一轮」那条护栏）。
    V3 把它删掉、把纯代数拆到 atoms.py / invariants.py，
    就是为了让「零 IO」变成**结构事实**而不是纪律。

    同样地，`migrate.py` 是**一次性迁移脚本**，不是业务路径：
    它可以读旧投影表，但不许变成日常读路径（正常读只认 ledger_legs）。
    """
    names = {p.name for p in LEDGER.glob("*.py")}
    assert "tree.py" not in names, "tree.py 已删除 —— 别把它复活"
    for required in ("amount.py", "atoms.py", "errors.py", "invariants.py",
                     "loader.py", "migrate.py", "ops.py", "service.py", "store.py"):
        assert required in names, f"账本包缺了 {required}"
    # loader.py 是账本侧唯一的只读 SQL 入口；service.py 之外的模块不许直连库
    for path in LEDGER.rglob("*.py"):
        if path.name in ("loader.py", "service.py", "store.py", "migrate.py"):
            continue
        for node in ast.walk(_tree(path)):
            for target in _import_targets(node):
                assert "backend.db" not in target, \
                    f"{path.name} 直连了 db —— 只读查询走 loader，写入走 store"
