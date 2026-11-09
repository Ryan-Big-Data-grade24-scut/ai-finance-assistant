# MCP 客户端接入与验证（AI Workflow V1.1）

本项目使用本机 MCP stdio Server。模型运行在用户选择的 AI 客户端中；项目不内置模型 API，也不接收模型 API Key。

## 1. 先完成服务端自检

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m backend.mcp.server --selftest
```

应显示 27 个工具已注册，并以 `[OK] selftest 全部通过` 结束。

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

该脚本会启动一个独立 MCP 子进程，执行 initialize、tools/list，并只读调用 `query_ledger_books`。它不会创建分类草案、修改交易或执行账本操作。

预期：

```text
✓ initialize：...
✓ tools/list：27 个工具全部可见
✓ tools/call：query_ledger_books 只读调用成功
[OK] MCP stdio 客户端握手、工具发现和只读调用全部通过
```

## 4. AI 对话恢复与多轮澄清

### 对话中断后恢复

1. 调用 `list_classification_proposals`，默认查询 draft。
2. 如果某项 `stale=true`，不要继续确认，重新获取分类批次。
3. 否则调用 `get_classification_proposal`，读取每笔建议和问题。

### 用户回答澄清问题

1. AI 将用户回答转换为明确的目标节点、置信度和理由。
2. 调用 `resolve_classification_clarification`，传入原 `proposal_id`、该笔 `suggestion` 和 `user_answer`。
3. 该工具只更新原草案，不写账本。
4. AI 重新展示更新后的草案。
5. 用户明确确认后，才调用 `confirm_classifications`。

### 规则冲突保护

如果同一交易命中多条指向不同分类的有效规则，服务端 `preview_classifications` 会拒绝直接分类，返回 `RULE_HINT_CONFLICT`。AI 必须先创建澄清项。不能只依赖提示词。

### 部分确认

`confirm_classifications` 的 `approved_tx_ids` 必须明确列出用户批准的交易。未批准项会在本份草案中标记为 rejected，不会暗中保留成待确认项。若用户之后改变主意，应重新读取批次并创建新草案。已批准部分可使用 `undo_classifications` 整体撤销。

## 5. 隐私和安全

- MCP Server 默认在本机 stdio 运行，不开放网络端口。
- 客户端模型可能会接收 AI 工具读取到的交易文本；使用前应检查客户端和模型服务的数据政策。
- 不要把真实数据库、原始账单、`.env` 或 API Key 放进 Git 或发给不可信的模型。
- AI 不得直接执行 SQL；所有账本写入仍经过 Classification Service 和 Ledger Engine。
