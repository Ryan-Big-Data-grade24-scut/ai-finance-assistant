from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXPECTED_TOOLS = {
    "query_summary", "query_calendar", "query_trend", "query_by_category",
    "query_by_method", "query_income_rank", "query_transactions", "get_tx_detail",
    "list_pending_classification", "set_override", "import_bill",
    "query_ledger_books", "query_ledger_tree", "apply_ledger_op",
    "query_classification_status", "query_classification_batch",
    "preview_classifications", "list_classification_proposals",
    "get_classification_proposal", "resolve_classification_clarification",
    "confirm_classifications", "reject_classifications", "undo_classifications",
    "list_classification_rules", "set_classification_rule_enabled",
    "update_classification_rule", "delete_classification_rule",
}


async def main() -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print(r"缺少 MCP SDK。请先执行：.\.venv\Scripts\python.exe -m pip install -r requirements.txt")
        return 2

    python = Path(sys.executable).resolve()
    server = StdioServerParameters(
        command=str(python),
        args=["-m", "backend.mcp.server"],
        env=None,
        cwd=str(ROOT),
    )
    print(f"启动 MCP 子进程：{python}")
    try:
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                print(f"✓ initialize：{init.serverInfo.name} {init.serverInfo.version}")
                result = await session.list_tools()
                names = {tool.name for tool in result.tools}
                missing = EXPECTED_TOOLS - names
                extra = names - EXPECTED_TOOLS
                if missing or extra:
                    print(f"✗ 工具列表不一致，missing={sorted(missing)} extra={sorted(extra)}")
                    return 1
                print(f"✓ tools/list：{len(names)} 个工具全部可见")
                read_result = await session.call_tool("query_ledger_books", {})
                if getattr(read_result, "isError", False):
                    print("✗ query_ledger_books 调用返回错误")
                    for item in getattr(read_result, "content", []):
                        if getattr(item, "text", None):
                            print(item.text)
                    return 1
                text_blocks = [
                    item.text for item in getattr(read_result, "content", [])
                    if getattr(item, "text", None)
                ]
                if not text_blocks:
                    print("✗ query_ledger_books 没有返回可读内容")
                    return 1
                print("✓ tools/call：query_ledger_books 只读调用成功")
                print("\n[OK] MCP stdio 客户端握手、工具发现和只读调用全部通过")
                return 0
    except Exception as exc:
        print(f"✗ MCP stdio 连接失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
