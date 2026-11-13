# 使用用户自己的 AI 进行智能记账（V1.0）

## 1. 工作方式

项目本身不内置模型。用户选择支持本地 stdio MCP 的 AI 客户端，模型负责理解自然语言和提出分类建议；项目负责读取真实账本、提供受限工具、验证建议、等待用户确认，并通过 Ledger Engine 落账。

```text
用户自己的 AI 客户端
        ↓ MCP stdio
backend/mcp/server.py
        ↓
Classification Service
查询批次 / 规则提示 / 草案 / 确认 / 规则管理 / 撤销
        ↓ 唯一写入口
Ledger Engine
        ↓
ledger_legs / ledger_history / Star Ledger Nebula
```

不使用内置模型 API，不保存模型密钥或对话内容。AI 客户端读取到的交易文本可能会发送给该客户端使用的模型服务，用户应自行确认其数据处理政策。

## 2. 连接 MCP

项目根目录 `mcp-config.example.json` 提供配置模板：

```json
{
  "mcpServers": {
    "ai-finance-assistant": {
      "command": "<项目绝对路径>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "backend.mcp.server"],
      "cwd": "<项目绝对路径>"
    }
  }
}
```

把路径替换为本机项目路径，然后将 `mcpServers` 内容复制到支持本地 stdio MCP 的客户端配置中。移动项目文件夹后，需要同步修改 `command` 和 `cwd`。客户端重启后应加载 27 个工具。可运行 `scripts/print_mcp_config.py` 自动生成当前机器的绝对路径配置。

先运行服务端自检和真实 stdio 握手：

```powershell
.venv\Scripts\python.exe -m backend.mcp.server --selftest
.venv\Scripts\python.exe scripts\check_mcp_connection.py
```

分别看到 `[OK] selftest 全部通过` 和 `[OK] MCP stdio 客户端握手、工具发现和只读调用全部通过` 才开始使用。

## 3. 建议的第一次对话

> 请连接 ai-finance-assistant。先调用 query_ledger_books 查看账本，再检查 star-live 的分类状态。每批读取 10 笔，只对有效金额大于 0 且可分类的交易提出建议。先展示草案和理由，不要未经我确认直接落账。遇到规则冲突或交易信息不足时先问我。

## 4. 分类工作流

1. `query_ledger_books`：选择账本。
2. `query_classification_status`：查看 eligible、pending、excluded_special 和规则数量。
3. `query_classification_batch`：读取交易、有效金额、平台补充字段、规则提示和允许节点。
4. `preview_classifications`：保存结构化草案，不修改账本。
5. AI 展示 ready 建议并逐个澄清不确定交易。
6. 用户回答后，调用 `resolve_classification_clarification` 更新原草案；该操作不修改账本。
7. 用户明确批准后，`confirm_classifications` 只提交批准的交易 ID；未批准项会明确标记为 rejected。
8. 如需撤销，`undo_classifications` 按整份 proposal 撤销；如果账本版本已变化，会拒绝覆盖后续操作。

### 对话中断恢复

- `list_classification_proposals` 默认列出 draft，可找回 proposal_id。
- `get_classification_proposal` 返回逐笔状态、当前路径和 stale 标志。
- `stale=true` 表示账本已变化，必须重新取批次生成新草案。

### 批次金额字段

- `amount`：原始交易金额。
- `refunded_amount`：已确认退款金额。
- `effective_amount`：退款抵扣后的有效金额。
- `is_partial_refund`：是否为部分退款原交易。
- `platform_details`：仅包含与分类相关的支付宝补充字段。

退款流水、全额退款交易、明确内部转账和退款证据冲突交易会被排除在 AI 分类批次之外，并通过 `excluded_special` 汇总说明原因。部分退款原交易仍会进入批次，但 AI 应保留原始分类，除非用户明确要求改变。

## 5. 用户规则记忆与管理

只有用户明确同意保存规则，且对应建议被确认后，规则才会写入数据库。规则只为未来批次提供 `rule_hints`，不静默改账。

支持字段：
- `counterparty`：交易对方
- `item`：商品说明
- `pay_method`：支付方式
- `source`：账单来源

规则管理工具：
- `list_classification_rules`：列出规则。
- `set_classification_rule_enabled`：启用或停用。
- `update_classification_rule`：修改匹配字段、关键词或目标节点。
- `delete_classification_rule`：删除规则。

若多条规则命中同一交易但指向不同分类，批次会返回 `rule_hint_conflict=true`。AI 必须询问用户，不得自行选择。服务端预览接口也会强制检查，不能只依赖提示词。

规则变更不会追溯修改已分类交易。若要重新分类，必须另行生成建议并由用户确认。

## 6. 微信与支付宝账单格式

本版本适配：
- 微信支付官方 `.xlsx`：通过“交易时间 / 交易类型 / 收/支 / 金额(元)”等表头识别。
- 支付宝官方 `.xlsx`：通过“交易号 / 交易创建时间 / 收/支 / 金额（元）”等表头识别。
- 支付宝 `.csv` 与加密 `.zip`：保留原有支持。

支付宝 Excel 的“成功退款（元）”“服务费（元）”“交易来源地”“资金状态”会保存在原始行 `raw_json` 中。分类批次只向 AI 暴露与分类有关的必要字段，不传工作簿账号和导出元信息。

导入采用交易号去重，重复导入不会重复写入。预览不会写库；提交后原始流水只新增，不更新。

## 7. 隐私和安全边界

- MCP Server 和 SQLite 在本机运行。
- 交易文本会被 AI 客户端读取，并可能发送到其模型服务。
- 项目不保存模型 API Key。
- AI 不能修改原始金额、时间、方向、订单号和原始流水。
- 所有分类写入必须通过 `preview_classifications` → 用户确认 → `confirm_classifications` → Ledger Engine。
- 不要将真实账单、数据库、`.env` 或 API 密钥提交到 Git。
