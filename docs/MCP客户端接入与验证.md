# MCP 客户端接入与验证（账本 V2）

本项目用本机 MCP stdio Server 把 V2 账本能力暴露给你自己的 AI 客户端。模型运行在客户端里；项目不内置模型 API，也不接收 API Key。Server 模块路径是 `backend.mcp.server`，和页面共用同一套 `backend/agentcore/registry.py` 工具定义。

## 1. 先完成服务端自检

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m backend.mcp.server --selftest
```

应显示 7 个业务工具，并通过工具契约、`read_skill`、直写重读、拒绝会话工具四项检查。

## 2. 自动生成客户端配置

必须从项目根目录运行，并使用项目自己的虚拟环境：

```powershell
.\.venv\Scripts\python.exe scripts\print_mcp_config.py --client claude
```

其他格式：

```powershell
.\.venv\Scripts\python.exe scripts\print_mcp_config.py --client cursor
.\.venv\Scripts\python.exe scripts\print_mcp_config.py --client vscode
.\.venv\Scripts\python.exe scripts\print_mcp_config.py --client generic
```

把输出的 JSON 片段合并到对应客户端的 MCP 配置中。不要把整个 JSON 文件覆盖到客户端已有配置里，以免删除其他 MCP Server。

### Claude Desktop / Cursor / 通用 mcpServers 格式

配置文件中使用 `mcpServers` 字段。Windows Claude Desktop 常见配置位置为 `%APPDATA%\Claude\claude_desktop_config.json`；Cursor 通常使用项目或用户级 `.cursor/mcp.json`。具体位置以客户端当前版本的设置页为准。

### VS Code 格式

VS Code 的 `mcp.json` 使用 `servers` 字段，脚本会生成对应结构。

## 3. 做一次真实 stdio 握手测试

```powershell
.\.venv\Scripts\python.exe scripts\check_mcp_connection.py
```

该脚本会启动独立的 MCP 子进程，执行 initialize、tools/list，调用一次只读 SQL 和一次 `read_skill`。它不会修改账本或导入数据。

预期：

```text
✓ initialize：ai-finance-ledger 0.2.0
✓ tools/list：7 个工具全部可见
✓ initialize：服务端提供了使用说明
✓ tools/call：只读 SQL 与 read_skill 调用成功
[OK] MCP stdio 客户端握手、工具发现和只读调用全部通过
```

## 4. 外部模型看到的工具

| 工具 | 用途 |
|---|---|
| `finance_report_brief_write` | 按当前周、月或年整封写入报告简报，金额用整数分，无法估计传 null |
| `finance_query_transactions` | 只读 SQL 查原始流水；表和字段见工具描述 |
| `finance_ledger_read` | 读 V2 账本树、交易归属、历史与操作范围 |
| `finance_ledger_apply` | 直接执行 V2 操作（初始化、移动、分组、净额组、镜像、卸载等） |
| `finance_import_read` | 读已解析的暂存账单与导入批次 |
| `finance_import_commit` | 把暂存批次写入原始流水；不自动初始化 V2 |
| `read_skill` | 读取操作顺序与具体做法（`finance-import`、`ledger-classify`、`ledger-initialize-uninstall`、`ledger-select-operate`） |

`initialize` 会返回一段 `instructions`：写操作直接执行并以回执为准，页面选区只是视图，导入入库和 V2 初始化是两步。复杂任务应先 `read_skill`，再按回执继续。

## 5. 隐私和安全

- MCP Server 默认在本机 stdio 运行，不开放网络端口。
- 客户端模型会接收工具读到的交易文本；使用前检查客户端和模型服务的数据政策。
- `finance_query_transactions` 只读；`finance_ledger_apply` 只通过 V2 引擎写账本，不改原始流水。
- 不要把真实数据库、原始账单、`.env` 或 API Key 放进 Git 或发给不可信的模型。

外部 MCP 进程启动时绑定账本库上次选中的账本。在网页切换账本后，请重连 MCP，使后续分析和写入都指向新账本。网页内置 ATLAS 会直接跟随账本切换。
