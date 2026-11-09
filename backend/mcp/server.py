"""
backend/mcp/server.py —— MCP 入口

原则与 main.py 完全相同：本文件【不含任何业务逻辑、不含任何 SQL】。
它只做一件事：把现有报表/账本业务函数翻译成 MCP 工具。

    AI 客户端 ──MCP──▶ 本文件 ──▶ reports/ · overrides.py · importer/

红线（见 docs/设计/第一轮/04_MCP.md）：
    本文件里出现 conn.execute / sqlite3 直接打回 —— 绕过 overrides.py
    就绕过了全项目唯一的 403 校验点，核心约束当场作废。

启动：
    python -m backend.mcp.server             # stdio，给 Claude Desktop 等客户端用
    python -m backend.mcp.server --selftest  # 不起服务，自检全部工具与关键闭环

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
from pydantic import TypeAdapter
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
from backend.classification import service as classification_service  # noqa: E402
from backend.classification.models import ClassificationSuggestion  # noqa: E402

mcp = FastMCP(
    "ai-finance-assistant",
    instructions=(
        "这是用户的个人财务账本。所有数字由后端从账本算出，"
        "你只负责解读与措辞，绝不心算、绝不改数字。\n"
        "口径因人而异（转给爸妈的不算、买理财不算、还花呗不算……）："
        "拿不准就先问清用户的口径，再用 set_override 落地成豁免/分类，"
        "不要替用户假设。\n"
        "原始记录永远不可修改 —— 试图传 amount / tx_time 等原始字段会被直接拒绝。"
        "\n做 AI 分类时必须遵守两阶段流程：先 query_classification_batch，"
        "再 preview_classifications；把草案、置信度和理由展示给用户，"
        "只有用户明确同意后才能调用 confirm_classifications。"
        "needs_clarification 的交易必须先提问，禁止猜测或直接确认。"
        "对话中断后先 list_classification_proposals 找回 draft，再 get_classification_proposal 恢复。"
        "用户回答澄清后调用 resolve_classification_clarification 更新原草案；这一步不落账。"
        "分类批次会排除退款流水、全额退款、明确内部转账和退款证据冲突交易；"
        "effective_amount 已扣除可核验退款。规则冲突时必须询问用户。"
    ),
)


# ═══════════════════════════════════════════════════════════
#  第一轮只读 9 个 —— 全部转发 reports/ 与 overrides 的读函数
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
    ledger_service.sync_live_book()
    return S.ImportCommitResp(
        batch_id=r.batch_id, inserted=r.inserted, skipped=r.skipped,
        failed=r.failed, first_error=r.first_error,
        date_from=r.date_from, date_to=r.date_to,
    )


# ═══════════════════════════════════════════════════════════
#  第二轮账本工具。AI 不得直接写 ledger_*，与前端使用同一个 service。
# ═══════════════════════════════════════════════════════════

@mcp.tool()
def query_ledger_books() -> dict:
    """列出星图账本和 book_id。账本不会自动合并，后续每次请求都必须传 book_id。"""
    return ledger_service.list_books()


@mcp.tool()
def query_ledger_tree(book_id: str, focus_node_id: str | None = None) -> dict:
    """读取账本的唯一权威树及其每笔交易归属、ADD/NET、审计结果。不要自己计算金额。"""
    return ledger_service.get_tree(book_id, focus_node_id)


@mcp.tool()
def apply_ledger_op(
    book_id: str, op: str,
    tx_id: int | None = None,
    to_node_id: str | None = None,
    parent_id: str | None = None,
    node_id: str | None = None,
    name: str | None = None,
    op_value: str | None = None,
    tx_ids: list[int] | None = None,
    expected_seq: int | None = None,
) -> dict:
    """把账本结构操作提交给统一引擎，绝不直接落库。

    reassign: tx_id + to_node_id（AI 粗分类唯一建议使用的写动作）；
    create_node: parent_id + name + op_value(ADD/NET) + tx_ids；
    set_op: node_id + op_value；dissolve_node: node_id；
    attach_tx/detach_tx: tx_id（attach_tx 可额外给 node_id）；
    split/unsplit: 仅适用于 funding 已显式设为“亲情卡 / 他人代付 / 外部额度”的交易；
                   退款和明确配对的自有账户内部转账不能分裂。split 会生成镜像腿并计入外部收入节点。
    建议读取 query_ledger_tree 再提交，传入 expected_seq 防止多窗口冲突。
    严禁改金额、时间、方向、原始流水。对大量交易不要逐笔无脑循环调用。
    """
    payload = {'op': op}
    supplied = {'tx_id': tx_id, 'to_node_id': to_node_id, 'parent_id': parent_id,
                'node_id': node_id, 'name': name, 'op_value': op_value, 'tx_ids': tx_ids}
    payload.update({key: val for key, val in supplied.items() if val is not None})
    validated = TypeAdapter(S.LedgerOpIn).validate_python(payload)
    return ledger_service.apply_op(book_id, validated.model_dump(), expected_seq)


# ═══════════════════════════════════════════════════════════
#  V3.1 用户自己的 AI：模型只提草案，确认后统一走 Ledger 引擎。
# ═══════════════════════════════════════════════════════════

@mcp.tool()
def query_classification_status(book_id: str = "star-live") -> dict:
    """读取 AI 分类进度：待审、已确认、草案和规则数量。

    开始分类任务时先调用一次。它不返回全量交易；接下来使用
    query_classification_batch，每处理完一批再取下一批。
    """
    return classification_service.get_status(book_id)


@mcp.tool()
def query_classification_batch(book_id: str = "star-live", limit: int = 20) -> dict:
    """取最多 50 笔尚未由用户确认的交易、可选节点及已确认规则提示。

    目标只能从 allowed_nodes 选择。看不准时不要猜：提交
    needs_clarification=true、target_node_id=null 和一个具体 question。
    每次确认后已审交易会自动离开队首，因此不需要自行维护分页游标。
    """
    return classification_service.get_batch(book_id, limit)


@mcp.tool()
def preview_classifications(
    book_id: str,
    suggestions: list[ClassificationSuggestion],
    expected_seq: int | None = None,
) -> dict:
    """保存结构化分类草案并做合法性校验；绝不修改账本。

    每条必须有 confidence 和 reason。明确建议提供 target_node_id；
    不明确建议必须改为 needs_clarification 并给 question。
    返回后先向用户展示 ready 和问题，禁止自动调用确认工具。
    """
    return classification_service.preview(book_id, suggestions, expected_seq)


@mcp.tool()
def list_classification_proposals(
    book_id: str = "star-live", status: str | None = "draft", limit: int = 20
) -> dict:
    """列出分类草案，适合 AI 对话中断后恢复工作。

    默认只列出 draft；status 可选 draft/applied/rejected/undone/null，
    最多返回 50 份。先调用本工具找回 proposal_id，再调用 get_classification_proposal。
    """
    return classification_service.list_proposals(book_id, status, limit)


@mcp.tool()
def get_classification_proposal(proposal_id: str) -> dict:
    """重新读取一份草案及状态，适合对话中断后继续确认。

    返回 ready_count、clarification_count、current_seq、stale 和逐笔分类路径。
    若 stale=true，不得继续确认或澄清，必须重新读取批次生成新草案。
    """
    return classification_service.get_proposal(proposal_id)


@mcp.tool()
def resolve_classification_clarification(
    proposal_id: str,
    suggestion: ClassificationSuggestion,
    user_answer: str,
    expected_seq: int | None = None,
) -> dict:
    """用户回答澄清问题后，更新原草案中的这一笔，不创建重复草案，也不落账。

    suggestion 必须给出明确 target_node_id、confidence 和 reason，
    needs_clarification 保持默认 false。user_answer 必须包含用户刚才的回答。
    更新后仍须向用户展示结果，并在用户明确批准后调用 confirm_classifications。
    """
    return classification_service.resolve_clarification(
        proposal_id, suggestion, user_answer=user_answer, expected_seq=expected_seq
    )


@mcp.tool()
def confirm_classifications(
    proposal_id: str,
    approved_tx_ids: list[int],
    expected_seq: int | None = None,
) -> dict:
    """仅在用户明确同意后，确认指定交易并原子地写入 Ledger。

    必须显式列出用户批准的 approved_tx_ids；未列出的建议会被拒绝。
    仍需澄清的交易不能确认。整批只生成一个 Ledger 修订，失败全部回滚。
    """
    return classification_service.confirm(proposal_id, approved_tx_ids, expected_seq)


@mcp.tool()
def reject_classifications(proposal_id: str, reason: str) -> dict:
    """用户不同意草案时关闭它；不会修改账本。"""
    return classification_service.reject(proposal_id, reason)


@mcp.tool()
def undo_classifications(proposal_id: str, expected_seq: int | None = None) -> dict:
    """整体撤销一份已确认分类，并停用由它产生的规则。

    为避免覆盖后续人工修改，只有账本仍停在该草案落账后的版本时才允许撤销。
    """
    return classification_service.undo(proposal_id, expected_seq)


@mcp.tool()
def list_classification_rules(book_id: str, include_disabled: bool = False) -> dict:
    """查看用户确认过的分类规则。规则只作为下一批的提示，不会静默改账。"""
    return classification_service.list_rules(book_id, include_disabled)


@mcp.tool()
def set_classification_rule_enabled(book_id: str, rule_id: str, enabled: bool) -> dict:
    """启用或停用用户规则；只改变规则状态，不会追溯修改任何交易。"""
    return classification_service.set_rule_enabled(book_id, rule_id, enabled)


@mcp.tool()
def update_classification_rule(
    book_id: str,
    rule_id: str,
    match_field: str | None = None,
    pattern: str | None = None,
    target_node_id: str | None = None,
) -> dict:
    """修改规则匹配字段、关键词或目标分类；不会自动重分类历史交易。"""
    return classification_service.update_rule(
        book_id, rule_id, match_field=match_field,
        pattern=pattern, target_node_id=target_node_id,
    )


@mcp.tool()
def delete_classification_rule(book_id: str, rule_id: str) -> dict:
    """删除一条用户规则；不删除已经完成的分类审核和账本历史。"""
    return classification_service.delete_rule(book_id, rule_id)


# ═══════════════════════════════════════════════════════════
#  自检 —— 不起服务，直接验证注册表和一条读写闭环
# ═══════════════════════════════════════════════════════════

EXPECTED_TOOLS = {
    "query_summary", "query_calendar", "query_trend",
    "query_by_category", "query_by_method", "query_income_rank",
    "query_transactions", "get_tx_detail", "list_pending_classification",
    "set_override", "import_bill",
    "query_ledger_books", "query_ledger_tree", "apply_ledger_op",
    "query_classification_status", "query_classification_batch",
    "preview_classifications", "list_classification_proposals",
    "get_classification_proposal", "resolve_classification_clarification",
    "confirm_classifications", "reject_classifications",
    "undo_classifications", "list_classification_rules",
    "set_classification_rule_enabled", "update_classification_rule",
    "delete_classification_rule",
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
    """注册表、防线、旧修正闭环和 V3.1 AI 两阶段分类闭环。"""
    import json
    import tempfile

    db.init_db()   # 新克隆环境可能还没有 bills.db，先幂等建表

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    if names != EXPECTED_TOOLS:
        print(f"✗ 工具注册表不符：{sorted(names)}")
        return 1
    print(f"✓ {len(names)} 个工具已注册：{sorted(names)}")

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

            # V3.1：验证外部 AI 只能先建草案，用户确认后才走 Ledger，且可整体撤销。
            ledger_service.sync_live_book()
            batch_res = await mcp.call_tool(
                "query_classification_batch", {"book_id": "star-live", "limit": 5}
            )
            batch = json.loads(_blocks_text(batch_res))
            if batch.get("pending_total") != 1 or not batch.get("allowed_nodes"):
                print(f"✗ AI 分类批次异常：{batch}")
                return 1
            tx = batch["items"][0]
            preview_res = await mcp.call_tool(
                "preview_classifications",
                {
                    "book_id": "star-live",
                    "expected_seq": batch["ledger_seq"],
                    "suggestions": [{
                        "tx_id": tx["tx_id"],
                        "target_node_id": tx["current_node_id"],
                        "confidence": 0.9,
                        "reason": "MCP 自检：保留当前分类",
                    }],
                },
            )
            preview = json.loads(_blocks_text(preview_res))
            if not preview.get("requires_user_confirmation"):
                print(f"✗ AI 草案没有进入待确认状态：{preview}")
                return 1
            confirm_res = await mcp.call_tool(
                "confirm_classifications",
                {
                    "proposal_id": preview["proposal_id"],
                    "approved_tx_ids": [tx["tx_id"]],
                    "expected_seq": batch["ledger_seq"],
                },
            )
            confirmed = json.loads(_blocks_text(confirm_res))
            if confirmed.get("status") != "applied":
                print(f"✗ AI 分类确认失败：{confirmed}")
                return 1
            await mcp.call_tool(
                "undo_classifications",
                {
                    "proposal_id": preview["proposal_id"],
                    "expected_seq": confirmed["ledger"]["current_seq"],
                },
            )
            if classification_service.get_status("star-live")["reviewed"] != 0:
                print("✗ AI 分类撤销后仍残留已确认记录")
                return 1
            print("✓ AI 分类闭环：查询 → 草案 → 用户确认 → Ledger 落账 → 整体撤销")

            # V1.1：对话中断恢复、澄清原草案更新、部分确认信息。
            resumed = await mcp.call_tool(
                "list_classification_proposals",
                {"book_id": "star-live", "status": "undone", "limit": 5},
            )
            resumed_data = json.loads(_blocks_text(resumed))
            if not resumed_data.get("proposals"):
                print("✗ list_classification_proposals 未能找回已撤销草案")
                return 1

            batch2 = classification_service.get_batch("star-live", limit=1)
            tx2 = batch2["items"][0]
            target2 = batch2["allowed_nodes"][0]["node_id"]
            clarify = classification_service.preview(
                "star-live",
                [{
                    "tx_id": tx2["tx_id"], "target_node_id": None,
                    "confidence": 0.2, "reason": "需要用户澄清。",
                    "needs_clarification": True,
                    "question": "这笔交易属于哪个类别？",
                }],
                expected_seq=batch2["ledger_seq"],
            )
            resolved_res = await mcp.call_tool(
                "resolve_classification_clarification",
                {
                    "proposal_id": clarify["proposal_id"],
                    "suggestion": {
                        "tx_id": tx2["tx_id"], "target_node_id": target2,
                        "confidence": 0.9, "reason": "根据用户回答归类。",
                    },
                    "user_answer": "这是用户确认的餐饮消费。",
                    "expected_seq": batch2["ledger_seq"],
                },
            )
            resolved_data = json.loads(_blocks_text(resolved_res))
            if resolved_data.get("clarification_count") != 0 or resolved_data.get("ready_count") != 1:
                print(f"✗ 澄清没有更新原草案：{resolved_data}")
                return 1
            print("✓ V1.1：草案可恢复，用户回答可写回原草案，仍未直接落账")
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
