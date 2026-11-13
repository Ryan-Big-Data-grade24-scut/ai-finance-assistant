# AI Finance Web V1.0 — 智能导入与分类校准

## 1. 正式需求

1. AI 在导入时分析整批交易，优先匹配现有分类；确实不合适时提出新分类。
2. 高置信度交易只有在命中用户明确启用的分类规则、分类节点存在、无规则冲突，并且用户开启自动规则授权时才自动落账。单纯模型自报高置信度不触发自动落账。
3. 用户可以通过网页对话校准当前导入批次；用户明确勾选后，才将当前确认保存为长期规则。
4. AI 不修改 `transactions_raw` 原始流水。所有正式分类操作经 `classification.service` 和 Ledger Engine。
5. 退款流水、全额退款、本人账户内部转账和退款证据冲突不进入普通 AI 分类。

## 2. 当前实现

- 模型协议：OpenAI-compatible Chat Completions、Anthropic Messages。
- 数据处理授权：默认关闭；用户明确同意后才会向配置的模型接口发送必要交易字段。
- API Key：Fernet 加密后存入 SQLite；主密钥在项目目录外的用户配置目录。
- AI 导入预览：解析文件后建立 24 小时暂存会话，不写原始交易表。
- 整批分析：先向模型发送批次分布与商户/商品分组，生成分类体系；再按最多 80 个唯一分组一批进行分类映射。
- 规则优先：唯一启用规则可以覆盖模型建议；多条规则指向不同分类时标记冲突。
- 历史学习：模型可参考最近最多 500 条已确认分类记录的聚合样例；不读取或发送交易订单号、银行卡号和完整原始账单。
- 新分类：只在用户批准对应分类路径后创建 Ledger group。
- 交易审核：用户勾选的交易进入确认流程；未勾选建议生成待审核草案。网页可查看草案、澄清、确认或拒绝。
- 安全重基：若无关账本操作使草案版本过期，网页端会先检查草案交易归属和目标节点；仅在全部未变化时安全更新版本。
- 长期规则：只有用户在当前导入审核界面勾选“保存”并确认该笔交易后才创建。
- 重复导入：AI 会话 ID 作为 import_batch 幂等键，避免重试重复写入。
- 兼容旧流程：未配置 AI 时，原 `/api/import/preview` 和 `/api/import/commit` 仍可使用。

## 3. 关键 API

- `GET /api/ai/settings`
- `PUT /api/ai/settings`
- `POST /api/ai/settings/test`
- `DELETE /api/ai/settings/key`
- `POST /api/ai/import/analyze`
- `GET /api/ai/import/{session_id}`
- `POST /api/ai/import/{session_id}/calibrate`
- `POST /api/ai/import/{session_id}/commit`
- `GET /api/ai/classification/proposals`
- `GET /api/ai/classification/proposals/{proposal_id}`
- `POST /api/ai/classification/proposals/{proposal_id}/resolve`
- `POST /api/ai/classification/proposals/{proposal_id}/confirm`
- `POST /api/ai/classification/proposals/{proposal_id}/reject`

## 4. 本地设置

1. 启动后端和前端。
2. 打开网页的“AI 设置”。
3. 选择模型协议，填写 Base URL、模型名称和 API Key。
4. 保存后点击“测试连接”。
5. 默认不启用自动规则落账。需要时，明确开启“允许已授权规则自动落账”。
6. 打开“导入账单”，上传 CSV/XLSX/ZIP，等待整批分析。
7. 检查 AI 建议分类体系，勾选同意创建的新分类。
8. 对需要修改的交易勾选确认，或通过批次对话发送校准指令。
9. 如需长期复用某条商户分类，勾选该交易的“保存”选项，再确认导入。

## 5. 数据与安全边界

- 本版本仍是本机单用户应用，没有实现账户登录和多租户隔离。
- 不要将后端 API 暴露到公网或不可信局域网。
- API Key 不会返回浏览器；加密主密钥在 `%APPDATA%/AI Finance Assistant/master.key`（Windows）或 `$XDG_CONFIG_HOME/AI Finance Assistant/master.key`（Linux/macOS）。
- 迁移数据库时，若没有同时迁移主密钥，API Key 将无法解密，需要重新填写。
- 暂存会话 24 小时过期；提交成功后暂存交易 JSON 清空，保留分类分析与会话记录。
- AI 置信度是模型自评，不是经过统计校准的概率。自动落账必须同时满足用户规则授权，不允许仅凭模型置信度自动执行。

## 6. 测试

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

## 7. 本轮验证记录

- Python：132 passed，2 个 FastAPI `on_event` 弃用警告。
- Ledger 黄金验收：全部通过。
- MCP selftest：当前构建环境未安装 `mcp` SDK，未执行；请在原项目虚拟环境运行。
- TypeScript API 模块：`frontend/src/api/ai.ts` 独立严格类型检查通过。
- Vite 构建：当前构建环境没有可用的前端依赖缓存，未执行；请在本地已有 `node_modules` 的项目目录运行 `npm run build`。
- 真实模型 API：没有用户 API Key，使用 mock provider 测试流程；需要用户配置后执行连接测试和真实账单验证。
