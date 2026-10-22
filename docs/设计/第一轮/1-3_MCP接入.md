# 1-3 · 功能三：MCP 接入

## 目标

**用户在自己的 AI 助手里就能操作这个账本。**

用户用 Claude、豆包或任何支持 MCP 的客户端，通过自然语言：

```
用户：9月花了多少？
用户：把这笔改成零食
用户：帮我在这个文件里导入微信账单
```

背后连的是我们的数据库，不需要打开网页。

---

## 提供 11 个工具

**只读 9 个**

| 工具 | 作用 |
|---|---|
| `query_summary` | 周期总额、环比 |
| `query_calendar` | 每日明细 |
| `query_trend` | 趋势与均值 |
| `query_by_category` | 分类统计 |
| `query_by_method` | 支出方式统计 |
| `query_income_rank` | 收入排行 |
| `query_transactions` | 明细查询（支持各类筛选） |
| `get_tx_detail` | 单笔明细 + 修正记录 |
| `list_pending_classification` | 还没人工分类过的交易 |

**可写 2 个**

| 工具 | 作用 |
|---|---|
| `import_bill` | 导入账单文件 |
| `set_override` | 修改一笔的分类 / 豁免 / 备注 |

---

## 一条硬规矩

**AI 可以改分类，但改不了原始记录。**

用户可以说「这笔是零食不是学习」，系统改的是标注，**账单原文永远不动**。

三条防线：

1. 工具的参数字段里根本没有原始字段名，AI 传不进来
2. 运行时黑名单再挡一次
3. 代码里不存在写原始表的路径

---

## 实现要点

**不要在 MCP 里自己写 SQL。**

```
❌ 在 backend/mcp/server.py 里直接 conn.execute("UPDATE ...")
✅ 在 backend/mcp/server.py 里调 overrides.set_override(...)
```

`overrides.py` 是唯一有校验的地方。绕过它，整个项目的核心约束就作废了。

参数模型直接复用 `backend/schemas.py`，MCP 的 schema 和 HTTP 的 schema 永远一致。

⚠️ 落地路径是 `backend/mcp/server.py`，不是根级 `mcp/server.py` ——
PyPI 上 SDK 的包名就叫 `mcp`，根级目录会遮蔽 SDK（详见 `04_MCP.md`）。

---

## 当前状态

**✅ 后端已实现**（`backend/mcp/server.py`，用 `python -m backend.mcp.server` 启动）。

- 11 个工具全部就位；`--selftest` 覆盖注册表 / 防线① / 读写闭环 / 403 与 404 语义
- stdio 真实客户端会话实测通过
- 待做：Claude Desktop 等真实客户端接入实测（README「跑起来」一节有配置示例）

---

## 延伸

`04_MCP.md`（边界与安全）· `02_后端.md` · `1-1_导入.md` · `1-2_生成报告.md`
