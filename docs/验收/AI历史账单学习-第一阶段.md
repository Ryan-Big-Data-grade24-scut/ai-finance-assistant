# AI 历史账单学习 · 第一阶段验收

## 已实现

- 历史交易状态统计。
- 后台持久化历史分析任务，支持页面轮询恢复。
- 读取现有账本交易并复用整批 AI 分类器。
- 已确认交易与人工分类覆盖保护。
- 退款、内部转账等特殊交易沿用现有排除逻辑。
- 新分类由用户批准后创建。
- 逐笔选择并通过分类服务 / Ledger Engine 应用历史分类。
- 可选择把商户或商品规则保存到现有规则库。
- 分块应用时记录已完成交易，支持相同请求幂等重试。

## 自动化测试

`tests/test_ai_history_learning.py` 覆盖：

1. 历史交易分析只生成预览，不直接改账。
2. 用户批准后通过 Ledger Engine 应用分类。
3. 原始交易行数保持不变。
4. 用户确认记录和长期规则正常保存。
5. 已有人工作业覆盖的交易受到保护。
6. 即使 AI 设置启用了自动规则，历史分析也不会静默落账。

## 本地验收命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_ledger_engine.py
.\.venv\Scripts\python.exe -m backend.mcp.server --selftest
```

前端：

```powershell
cd frontend
npm run build
```

## 限制

当前开发容器没有完整的前端 npm 依赖，因此此处未完成 `vue-tsc` / Vite 生产构建。请在用户本地已有 `node_modules` 的环境运行 `npm run build` 验证。
