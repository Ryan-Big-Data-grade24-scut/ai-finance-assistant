# AI Workflow V1.1 — 验收说明

## 本轮目标

在 AI Intelligence V1.0 的 25 个 MCP 工具和两阶段分类流程上，补齐多轮澄清、草案恢复与服务端强制规则冲突保护，并提供本机 MCP 客户端配置和真实 stdio 冒烟测试工具。

## 已完成

1. 新增 `list_classification_proposals`，可按状态恢复分类草案。
2. `get_classification_proposal` 增加 `current_seq`、`stale`、`ready_count`、`clarification_count` 和逐笔分类路径。
3. 新增 `resolve_classification_clarification`，用户回答后更新原 proposal，不新建重复草案、不直接修改账本。
4. 账本版本变化后，草案会被标记为 stale；过期草案不能继续澄清或确认。
5. 服务端 `preview` 强制拦截指向不同分类的规则冲突，返回 `RULE_HINT_CONFLICT`。
6. 规则记忆关键词必须真实出现在对应交易字段中，避免 AI 写入无效或错配规则。
7. 部分确认返回 `approved_tx_ids` 与 `rejected_tx_ids`，明确说明未批准项在当前 proposal 中关闭。
8. MCP 工具总数由 25 增至 27，工具名与 `EXPECTED_TOOLS` 静态一致。
9. 新增 `scripts/print_mcp_config.py`，根据当前项目路径和 Python 解释器生成 Claude Desktop、Cursor、VS Code 或通用 MCP 配置。
10. 新增 `scripts/check_mcp_connection.py`，使用 MCP Client SDK 启动独立 stdio 子进程，验证 initialize、tools/list 和只读 tools/call。

## 测试结果（当前构建环境）

- Python 全量测试：120 passed，2 warnings。
- Ledger 黄金验收：全部通过。
- 新增 AI 工作流测试：8 项通过。
- MCP 工具 AST 契约：27 个装饰函数与 `EXPECTED_TOOLS` 完全一致。
- Python 编译检查：通过。
- MCP SDK 未安装在当前构建环境，无法在此处启动真实 stdio MCP 子进程；请在用户本地 `.venv` 中运行 `scripts/check_mcp_connection.py`。
- 外部 AI 客户端（Claude Desktop / Cursor / 其他客户端）尚未由本构建环境实际连接，需在用户选择客户端后完成最终联调。

## 本地验收命令

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_ledger_engine.py
.\.venv\Scripts\python.exe -m backend.mcp.server --selftest
.\.venv\Scripts\python.exe scripts\check_mcp_connection.py
```

配置生成示例：

```powershell
.\.venv\Scripts\python.exe scripts\print_mcp_config.py --client claude
.\.venv\Scripts\python.exe scripts\print_mcp_config.py --client vscode
```

## 数据库与兼容性

- 未新增数据库表或字段。
- 不需要修改 `db/schema.sql`。
- 不需要运行数据库迁移。
- 未修改 `transactions_raw`、`ledger_legs` 或账本历史结构。
- 原有分类草案和规则表继续使用。

## 安全边界

- 澄清写回只更新草案，不写 Ledger。
- 用户明确确认后才通过 Ledger Engine 落账。
- 冲突规则不能靠模型提示词绕过服务端校验。
- 过期 proposal 不允许覆盖后续账本版本。
- MCP Server 默认使用本机 stdio，不开放网络端口。
- 用户交易文本可能被所选 AI 客户端发送到其模型服务，用户需自行确认该客户端的数据处理政策。
