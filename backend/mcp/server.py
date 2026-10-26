"""
backend/mcp/server.py —— MCP 入口

原则与 main.py 完全相同：本文件【不含任何业务逻辑、不含任何 SQL】。
它只做一件事：把 11 个业务函数翻译成 MCP 工具。

    AI 客户端 ──MCP──▶ 本文件 ──▶ reports/ · overrides.py · importer/

红线（见 docs/设计/第一轮/04_MCP.md）：
    本文件里出现 conn.execute / sqlite3 直接打回 —— 绕过 overrides.py
    就绕过了全项目唯一的 403 校验点，核心约束当场作废。

启动：
    python -m backend.mcp.server             # stdio，给 Claude Desktop 等客户端用
    python -m backend.mcp.server --selftest  # 不起服务，自检 11 个工具

接入客户端（如 Claude Desktop）的配置示例：
    {
      "mcpServers": {
        "ai-finance-assistant": {
          "command": "<项目路径>/.venv/Scripts/python.exe",
          "args": ["-m", "backend.mcp.server"],
          "cwd": "<项目路径>"
        }
      }
    }
"""
from __future__ import annotations

import sys
from pathlib import Path

# 直接以脚本方式启动时（如客户端配置的绝对路径），项目根不一定在 sys.path。
# main.py 用同样的手法，见 backend/main.py:26。
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 控制台编码容错（Windows 必做）─────────────────────────────
# --selftest 里有一堆 ✓ / ✗ 打印。Windows 控制台默认 GBK 编不出这些字符，
# 一旦碰到就是 UnicodeEncodeError 直接崩 —— main.py 已经踩过同一个坑。
# 这里兜住：编不出来就替换，绝不让打印把自检或服务干掉。
#
# ⚠️ 不影响 MCP 协议通道：SDK 走的是 TextIOWrapper(sys.stdout.buffer, encoding="utf-8")，
#    是套在 buffer 上的新包装器，不受这里对 sys.stdout 本身的重配置影响。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # 非标准流（被测试捕获等）直接跳过
        pass

from mcp.server.fastmcp import FastMCP  # noqa: E402

from backend import db, overrides, reports  # noqa: E402
from backend import schemas as S  # noqa: E402
from backend.importer import import_file  # noqa: E402
from backend.ledger import service as ledger_service  # noqa: E402

mcp = FastMCP(
    "ai-finance-assistant",
    instructions=(
        "这是用户的个人财务账本。所有数字由后端从账本算出，"
        "你只负责解读与措辞，绝不心算、绝不改数字。\n"
        "口径因人而异（转给爸妈的不算、买理财不算、还花呗不算……）："
        "拿不准就先问清用户的口径，再用 set_override 落地成豁免/分类，"
        "不要替用户假设。\n"
        "原始记录永远不可修改 —— 试图传 amount / tx_time 等原始字段会被直接拒绝。\n"
        "账本（query_ledger_* / apply_ledger_op）是**另一套口径**，"
        "和上面的报表数字不是一回事，不要相加也不要互相校验；"
        "账本里的每个数都由后端算出，你只解读、不心算。"
    ),
)


# ═══════════════════════════════════════════════════════════
#  只读 9 个 —— 全部转发 reports/ 与 overrides 的读函数
#  docstring 是 AI 看到的说明书：写清「什么时候用 / 别怎么用」，
#  这是服务端控制 agent loop 调用频率的主要手段。
# ═══════════════════════════════════════════════════════════

@mcp.tool()
def query_summary(req: S.ReportReq) -> S.SummaryResp:
    """周期总览：本期合计、上期合计、环比。

    回答「这个月/这周/今年花了多少」用这一个工具就够，
    不要再逐日调 query_calendar 或逐期循环调用本工具拼凑。
    """
    return reports.get_summary(req)


@mcp.tool()
def query_calendar(req: S.ReportReq) -> S.CalendarResp:
    """每日合计（无消费的日期不返回，不是 0）。

    看「钱花在哪几天」用这个；看跨期趋势请用 query_trend，
    不要逐月循环调用本工具。
    """
    return reports.get_calendar(req)


@mcp.tool()
def query_trend(req: S.ReportReq, n: int = 5) -> S.TrendResp:
    """近 n 期柱状趋势 + 均值线（n 最多 24）。

    回答「最近几个月花得多不多」用这个，不要自己循环 query_summary 拼。
    """
    return reports.get_trend(req, n=n)


@mcp.tool()
def query_by_category(req: S.ReportReq) -> S.CategoryResp:
    """分类占比：大类 + 小类两级。

    要下钻某类的逐笔明细时，把大类名传给 query_transactions 的
    category 参数，而不是反复调用本工具。
    """
    return reports.get_by_category(req)


@mcp.tool()
def query_by_method(req: S.ReportReq) -> S.MethodResp:
    """支出方式分组，按支付方式原始字符串（不做归并）。只对支出有意义。"""
    return reports.get_by_method(req)


@mcp.tool()
def query_income_rank(req: S.ReportReq) -> S.IncomeRankResp:
    """本期收入排行 TOP50（按金额降序）。req.type 被固定为收入。"""
    return reports.get_income_rank(
        S.ReportReq(type="income", period=req.period, value=req.value)
    )


@mcp.tool()
def query_transactions(q: S.TxQuery) -> S.PaginatedResp:
    """明细列表，支持分类/支付方式/关键词/日期区间等筛选。

    回答总量类问题优先用聚合工具（query_summary / query_by_category），
    确实要看逐笔时再用本工具。先看返回的 total 再决定要不要翻页
    （page/size，单页最多 200 条），不要一次拉全量。
    """
    return reports.query_transactions(q)


@mcp.tool()
def get_tx_detail(tx_id: int) -> S.TxDetailResp:
    """单笔全貌：原始记录 + 修正记录并排。

    用 set_override 修改之前，先用本工具核对 tx_id 和原始内容。
    """
    return overrides.get_tx_detail(tx_id)


@mcp.tool()
def list_pending_classification(limit: int = 20) -> S.PendingResp:
    """还没被人工/AI 修正过的交易清单（按时间倒序，最多 50 条）。

    帮用户分类时从这里拿候选；被改过的笔会自动从清单里消失。
    """
    return reports.list_pending(limit)


# ═══════════════════════════════════════════════════════════
#  可写 2 个 —— 修正走 overrides.py（唯一入口），导入走 importer/
#  全部带 updated_by="mcp"，三个月后能查出「这笔是 AI 改的」
# ═══════════════════════════════════════════════════════════

@mcp.tool()
def set_override(tx_id: int, data: S.OverrideIn, clear: bool = False) -> dict:
    """修改一笔交易的分类 / 豁免 / 资金来源 / 备注；clear=true 撤销修正。

    只接受这 6 个修正字段。金额 / 时间 / 方向等原始字段不可修改，
    传了会被拒绝（403）—— 这是设计，不是故障。
    修改前先用 get_tx_detail 核对。
    """
    if clear:
        overrides.clear_override(tx_id, updated_by="mcp")
        return {"tx_id": tx_id, "cleared": True, "updated_by": "mcp"}
    resp = overrides.set_override(tx_id, data, updated_by="mcp")
    return resp.model_dump()


@mcp.tool()
def import_bill(path: str,
                platform_id: str | None = None,
                alipay_password: str | None = None) -> S.ImportCommitResp:
    """导入账单文件（微信 .xlsx / 支付宝导出文件），全事务，失败整体回滚。

    幂等：重复导入自动按交易单号去重，返回的 skipped 是跳过的重复笔数，
    skipped 明显偏大时提醒用户人工核查。path 必须是本机账单文件的实际路径。
    alipay_password 仅在解压支付宝 ZIP 时需要；不要把它写进任何日志或回复。
    """
    r = import_file(Path(path), platform_id=platform_id,
                    alipay_password=alipay_password)
    return S.ImportCommitResp(
        batch_id=r.batch_id, inserted=r.inserted, skipped=r.skipped,
        failed=r.failed, first_error=r.first_error,
        date_from=r.date_from, date_to=r.date_to,
    )


# ═══════════════════════════════════════════════════════════
#  账本 3 个 —— 第二轮「账本代数引擎」
#
#  ⚠️ 账本的数字和上面 9 个报表工具的数字**不是一回事**：
#     报表是平台口径的平铺统计，账本是用户自己分的组。两者不相加。
#
#  为什么用扁平可选参数而不是判别联合：
#     MCP 的工具 schema 来自函数签名。判别联合会变成 oneOf，
#     模型选分支时容易出错。扁平参数 + 一个 op 字符串更稳。
# ═══════════════════════════════════════════════════════════

@mcp.tool()
def query_ledger_books() -> list:
    """账本列表：用户定义的若干「阶段」，每阶段一本独立账，各有自己的树。

    每本账的成员集在建账时按日期区间**冻结**了，互不重叠也不相加。
    要看某一本的树，用 query_ledger_tree(book_id)。
    """
    return [
        {
            "book_id": b.book_id,
            "name": b.name,
            "date_from": b.date_from,
            "date_to": b.date_to,
            "created_at": b.created_at,
        }
        for b in ledger_service.list_books()
    ]


@mcp.tool()
def query_ledger_tree(book_id: str, focus_node_id: str | None = None) -> dict:
    """账本的整棵树：节点、每个节点的 (支出, 收入)、腿的归属。

    focus_node_id 只影响返回里的 focus_node_ids 提示，不裁剪内容。
    返回的 tree 就是**权威状态** —— 不要自己维护一份树再和它对比。
    """
    return ledger_service.get_tree(book_id, focus_node_id)


@mcp.tool()
def apply_ledger_op(
    book_id: str,
    op: str,
    node_id: str | None = None,
    tx_id: int | None = None,
    to_node_id: str | None = None,
    name: str | None = None,
    op_value: str | None = None,
    tx_ids: list[int] | None = None,
) -> dict:
    """对账本做一次修改（**唯一的写通道**）。

    op 取值与所需参数：

    - "set_op"          切节点的聚合方式。node_id=目标节点，op_value="ADD"|"NET"
    - "split"           分裂一笔交易。tx_id=交易 id
                        ⚠️ 只对「涉及外部额度」的交易有效（亲情卡/他人代付），
                           别的会被拒绝 —— 那是设计，不是故障
    - "unsplit"         把分裂的两条腿合回一条。tx_id=交易 id
    - "create_node"     建分组。parent_id 走 node_id，name=分组名，
                        op_value=新节点的聚合方式，tx_ids=要挪进去的交易 id 列表
    - "dissolve_node"   解散分组。node_id=目标节点（成员会回到父节点）
    - "reassign"        改交易归属。tx_id=交易 id，to_node_id=目标节点
    - "attach_tx"       把一笔交易纳入本账。tx_id=交易 id（node_id 可选）
    - "detach_tx"       把一笔交易移出本账。tx_id=交易 id

    怎么用：

    - **别逐笔调用做批量归类。** 想把 20 笔挪进同一个组，用一次
      create_node 带 tx_ids=[...]，不要循环 20 次 reassign。
    - **别改金额、时间、方向。** 账本层管不了这些 —— 那些是原始事实。
    - 返回值里的 tree 就是权威状态，直接用它，不要自己推算数字。
    - 引擎会拒绝非法组合并给出一句人话理由 —— 把那句话转述给用户，
      不要自己另编一套解释。
    """
    payload: dict = {"op": op}
    if node_id is not None:
        # 建分组时 node_id 表示**父节点**；其它 op 里它就是目标节点
        payload["parent_id" if op == "create_node" else "node_id"] = node_id
    if tx_id is not None:
        payload["tx_id"] = tx_id
    if to_node_id is not None:
        payload["to_node_id"] = to_node_id
    if name is not None:
        payload["name"] = name
    if op_value is not None:
        payload["op_value"] = op_value
    if tx_ids is not None:
        payload["tx_ids"] = list(tx_ids)
    return ledger_service.apply_op(book_id, payload, created_by="mcp")


# ═══════════════════════════════════════════════════════════
#  自检 —— 不起服务，直接验证注册表和一条读写闭环
# ═══════════════════════════════════════════════════════════

EXPECTED_TOOLS = {
    "query_summary", "query_calendar", "query_trend",
    "query_by_category", "query_by_method", "query_income_rank",
    "query_transactions", "get_tx_detail", "list_pending_classification",
    "set_override", "import_bill",
    "query_ledger_books", "query_ledger_tree", "apply_ledger_op",
}


def _walk_schema_props(schema: dict, keys: set | None = None) -> set:
    """递归收集 JSON Schema 里所有 properties 的字段名。"""
    keys = set() if keys is None else keys
    for name, sub in (schema.get("properties") or {}).items():
        keys.add(name)
        if isinstance(sub, dict):
            _walk_schema_props(sub, keys)
    for sub in (schema.get("$defs") or {}).values():
        if isinstance(sub, dict):
            _walk_schema_props(sub, keys)
    return keys


def _blocks_text(res) -> str:
    """从 call_tool 的返回里取文本。1.x 各小版本返回形态不一：
    CallToolResult(.content) / (内容块, structured) 元组 / 裸内容块列表。
    """
    if hasattr(res, "content"):
        blocks = res.content
    elif isinstance(res, tuple):
        blocks = res[0]
    else:
        blocks = res
    return "\n".join(getattr(c, "text", "") for c in blocks)


async def _selftest() -> int:
    """四步：注册表 → 防线① → 读闭环 → 写闭环（在一次性临时库上）。"""
    import tempfile

    db.init_db()   # 新克隆环境可能还没有 bills.db，先幂等建表

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    if names != EXPECTED_TOOLS:
        print(f"✗ 工具注册表不符：{sorted(names)}")
        return 1
    print(f"✓ {len(EXPECTED_TOOLS)} 个工具已注册：{sorted(names)}")

    so = next(t for t in tools if t.name == "set_override")
    leaked = _walk_schema_props(so.inputSchema) & overrides.FORBIDDEN_FIELDS
    if leaked:
        print(f"✗ 防线①失守：set_override 的 schema 泄漏了原始字段 {sorted(leaked)}")
        return 1
    print("✓ 防线①：set_override 的协议 schema 不含任何原始字段")

    # 读闭环：查真账本（只读，空库也返回 0，不报错）
    try:
        res = await mcp.call_tool(
            "query_summary",
            {"req": {"type": "expense", "period": "month", "value": "2026-09"}},
        )
    except Exception as e:
        print(f"✗ query_summary 调用失败：{e}")
        return 1
    text = _blocks_text(res)
    if "total" not in text:
        print(f"✗ query_summary 返回异常：{text[:200]}")
        return 1
    print(f"✓ 读闭环：query_summary → {text.strip()[:80]}")

    # 写闭环：临时库上 落一条修正 + 验证 403 防线 + 验证待分类清单联动
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "selftest.db"
        db.init_db(tmp_db)
        real_path, db.DB_PATH = db.DB_PATH, tmp_db
        try:
            conn = db.get_conn(tmp_db)
            conn.execute(
                "INSERT INTO platforms (id, name, kind, adapter) "
                "VALUES ('wechat', '微信支付', 'wallet', 'wechat')"
            )
            conn.execute(
                "INSERT INTO transactions_raw "
                "(source, platform_id, tx_time, direction, amount, date, month,"
                " year, weekday, hour, dedupe_key, raw_json) "
                "VALUES ('微信支付','wechat','2026-09-01 12:00:00','支出',12.5,"
                "'2026-09-01','2026-09','2026',1,12,'selftest|t1','{}')"
            )
            conn.commit()
            conn.close()

            pending = reports.list_pending()
            if pending.total != 1:
                print(f"✗ list_pending 应为 1 笔，实际 {pending.total}")
                return 1

            await mcp.call_tool(
                "set_override",
                {"tx_id": 1, "data": {"manual_category": "餐饮", "is_exempt": True}},
            )
            conn = db.get_conn(tmp_db)
            row = conn.execute(
                "SELECT manual_category, updated_by FROM tx_overrides WHERE tx_id=1"
            ).fetchone()
            conn.close()
            if row is None or row["updated_by"] != "mcp":
                print(f"✗ 修正未落库或来源标记错误：{dict(row) if row else None}")
                return 1
            print(f"✓ 写闭环：修正已落库，updated_by={row['updated_by']}")

            if reports.list_pending().total != 0:
                print("✗ 修正后 list_pending 应为 0 笔")
                return 1
            print("✓ 修正后该笔已从待分类清单消失")

            try:
                await mcp.call_tool(
                    "set_override",
                    {"tx_id": 1, "data": {"amount": 999}},
                )
                print("✗ 传入原始字段 amount 竟然成功 —— 防线失守")
                return 1
            except Exception as e:
                if "amount" not in str(e):
                    print(f"✗ 拒绝原因不对：{e}")
                    return 1
            print("✓ 防线②：传入原始字段被拒（extra=forbid 校验生效）")

            try:
                await mcp.call_tool("get_tx_detail", {"tx_id": 999})
                print("✗ 不存在的 tx_id 竟然成功")
                return 1
            except Exception as e:
                if "交易不存在" not in str(e):
                    print(f"✗ 404 语义丢失：{e}")
                    return 1
            print("✓ 404 语义：不存在的交易返回「交易不存在: 999」")

            # ── 账本 3 步 ───────────────────────────────────
            # 建账走 HTTP 层的 service（MCP 工具里没有「建账」——
            # 建账是用户行为，不是 AI 的活儿）
            lb = ledger_service.create_book("自检账", "2026-09-01", "2026-09-30")
            lbid = lb["book"]["book_id"]

            books = await mcp.call_tool("query_ledger_books", {})
            if lbid not in _blocks_text(books):
                print(f"✗ query_ledger_books 返回异常：{_blocks_text(books)[:200]}")
                return 1
            print("✓ 账本读闭环①：query_ledger_books 正常返回")

            created = await mcp.call_tool(
                "apply_ledger_op",
                {"book_id": lbid, "op": "create_node",
                 "node_id": lb["root_id"], "name": "测试组",
                 "op_value": "ADD", "tx_ids": [1]},
            )
            ctext = _blocks_text(created)
            if "nodes" not in ctext:
                print(f"✗ apply_ledger_op 返回异常：{ctext[:300]}")
                return 1
            print("✓ 账本写闭环：apply_ledger_op 建组成功，返回权威树")

            tree = await mcp.call_tool("query_ledger_tree", {"book_id": lbid})
            if "root_id" not in _blocks_text(tree):
                print(f"✗ query_ledger_tree 返回异常：{_blocks_text(tree)[:200]}")
                return 1
            print("✓ 账本读闭环②：query_ledger_tree 返回整棵树")

            try:
                await mcp.call_tool(
                    "apply_ledger_op",
                    {"book_id": lbid, "op": "split", "tx_id": 1},
                )
                print("✗ 不该分裂的交易竟然分裂成功 —— 适用域失守")
                return 1
            except Exception as e:
                if "外部额度" not in str(e):
                    print(f"✗ 拒绝理由不对：{e}")
                    return 1
            print("✓ 引擎拒绝语义：分裂不适用时拒绝并给出人话理由")
        finally:
            db.DB_PATH = real_path

    print("\n[OK] selftest 全部通过")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import asyncio
        raise SystemExit(asyncio.run(_selftest()))
    # stdio 模式：stdout 是协议通道，这里严禁 print 任何东西
    db.init_db()
    mcp.run()
