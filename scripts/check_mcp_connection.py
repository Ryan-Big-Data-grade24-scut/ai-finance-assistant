from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK，勾叉和中文不能把自检本身弄崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXPECTED_TOOLS = {
    "finance_query_transactions", "finance_ledger_read", "finance_ledger_apply",
    "finance_import_read", "finance_import_commit", "finance_report_brief_write", "read_skill",
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
                if init.instructions:
                    print("✓ initialize：服务端提供了使用说明")
                read_result = await session.call_tool(
                    "finance_query_transactions", {"sql": "SELECT 1 AS ok"}
                )
                if getattr(read_result, "isError", False):
                    print("✗ finance_query_transactions 调用返回错误")
                    for item in getattr(read_result, "content", []):
                        if getattr(item, "text", None):
                            print(item.text)
                    return 1
                text_blocks = [
                    item.text for item in getattr(read_result, "content", [])
                    if getattr(item, "text", None)
                ]
                if not text_blocks:
                    print("✗ finance_query_transactions 没有返回可读内容")
                    return 1
                skill = await session.call_tool("read_skill", {"skill_id": "ledger-classify"})
                if getattr(skill, "isError", False):
                    print("✗ read_skill 调用返回错误")
                    for item in getattr(skill, "content", []):
                        if getattr(item, "text", None):
                            print(item.text)
                    return 1
                print("✓ tools/call：只读 SQL 与 read_skill 调用成功")
                print("\n[OK] MCP stdio 客户端握手、工具发现和只读调用全部通过")
                return 0
    except Exception as exc:
        print(f"✗ MCP stdio 连接失败：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
