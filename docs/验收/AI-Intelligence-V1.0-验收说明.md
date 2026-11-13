# AI Intelligence V1.0 — 验收说明

## 本轮范围

本轮优先完成“用户自己的 AI + MCP”基础，不内置模型 API，也不把模型密钥放入项目。

### 已完成

1. 微信官方 XLSX 表头识别。
2. 支付宝官方 XLSX 表头识别与导入。
3. 支付宝原始行保留交易来源地、资金状态、服务费、成功退款金额等字段。
4. 分类批次返回原始金额、退款抵扣金额、有效金额和平台补充信息。
5. 退款流水、全额退款原交易、明确内部转账、退款证据冲突交易从 AI 分类批次排除。
6. `preview` 对上述特殊交易执行服务端二次拦截，不能只依赖 AI 遵守 prompt。
7. 用户规则支持查看、启用/停用、修改和删除。
8. 多条用户规则指向不同节点时，批次显式返回 `rule_hint_conflict=true`。
9. 更新 AI 分类助手指令和外部 MCP 客户端使用说明。

## 测试结果

- 完整 Python 测试：110 passed，2 warnings。
- 用户上传的微信 XLSX：100 笔，解析失败 0，支出/收入/中性金额与账单汇总一致。
- 用户上传的支付宝 XLSX：18 笔，解析失败 0，支出金额与账单记录合计 775.17 元一致。
- 两份真实 XLSX 通过 `/api/import/preview` 和 `/api/import/commit` 集成验证，写入临时数据库共 118 笔；没有修改用户的本地数据库。
- Ledger Engine 黄金验收脚本：全部通过。
- MCP 源码静态检查：25 个 `@mcp.tool()` 与 `EXPECTED_TOOLS` 完全一致；Python 编译检查通过。

## 本地验收命令

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_ledger_engine.py
.\.venv\Scripts\python.exe -m backend.mcp.server --selftest
```

预期：
- pytest：110 passed，2 warnings。
- Ledger 黄金验收：所有验收项通过。
- MCP selftest：25 个工具已注册，`[OK] selftest 全部通过`。

## 本轮未包含

- 未内置 OpenAI / Anthropic 等模型 API。
- 未新增网页内置聊天界面。
- 未新增数据库表或迁移脚本。
- 未修改原始交易表结构。
- 未把用户上传的真实微信/支付宝账单复制进补丁。

## 安全边界

- AI 只能提交分类草案。
- 特殊交易不能通过分类预览绕过服务端校验。
- 用户明确确认后才通过 Ledger Engine 落账。
- 规则管理只影响未来规则提示，不追溯修改历史交易。
- 原始流水和原始金额不可由 AI 修改。
