# 04 · MCP

> 本文只说 **MCP 这一层的边界与安全**。
> 工具清单、对话流程、验收标准在 `1-3_MCP接入.md`。

---

## 一、已有的是什么

后端已经是一套完整的 HTTP API，12 个接口，AI 完全可以自己去发 HTTP 请求。

## 存在什么问题没解决

### 问题 1：AI 为什么要「塞进来」

传统做法是把 AI 嵌进 App：AI 和数据库、界面绑死。

**用户只能用这个 App 预设的问法。** 想换个角度问、换个说法问，都做不到——只能等 App 版本更新。

我们想做的是反过来的：**AI 在用户自己的客户端里，账本不动。**

### 问题 2：AI 直连数据库会绕过所有约束

如果给 AI 一个数据库连接，它能 `UPDATE transactions_raw SET amount = 3.2`。

**整个项目的核心卖点当场作废。**

### 问题 3：AI 怎么知道有哪些工具

AI 连接时读工具 schema 来决定「有哪些工具、参数叫什么」。schema 靠什么生成？靠 `schemas.py`。

这一条如果没做，AI 根本不知道我们有什么能力——**功能存在但用不上**。

---

## 二、提出的设计

### 设计 1：MCP Server 不自己写 SQL

```
┌──────────────────┐
│  用户的 AI        │  Claude / 豆包 / 任何 MCP 客户端
│（不在我们应用里）  │
└────────┬─────────┘
         │ MCP
┌────────▼───────────────┐
│ backend/mcp/server.py  │
└────────┬───────────────┘
         │ 调用同一批 Python 函数
   ┌─────┴─────┐
   ▼           ▼
reports/   overrides.py
```

> **为什么在 `backend/` 下而不是文档早期写的根级 `mcp/`：**
> PyPI 上 SDK 的包名就叫 `mcp`，项目根放一个 `mcp/` 目录会把 SDK 遮蔽，
> `import mcp` 直接崩。子包 `backend.mcp` 不冲突。`requirements.txt` 里
> `mcp` 同时钉死了 `<2`（2.x 把 FastMCP 改名为 MCPServer，现有写法会失效）。

**MCP 只做转发，不做数据访问。**

理由：`overrides.py` 是唯一有 403 校验的地方。BYPASS 它在 MCP 里直接写 SQL，等于把整个项目的核心约束作废。

### 设计 2：写权限只有 2 个工具

11 个工具里只有 2 个能写：

| 工具 | 写入目标 |
|---|---|
| `import_bill` | `transactions_raw`（**只 INSERT**） |
| `set_override` | `tx_overrides` |

其余 9 个全部只读。

**AI 面对 50 个工具会懵，面对 11 个会用。** 工具数量本身就是设计。

### 设计 3：schema 从 `schemas.py` 派生

```python
@mcp.tool()
def set_override(tx_id: int, data: schemas.OverrideIn) -> schemas.OverrideResp:
    """修改一笔交易的分类/豁免/备注。原始字段不可修改。"""
    return overrides.set_override(tx_id, data, updated_by="mcp")
```

**MCP 的工具 schema 和 HTTP 的 schema 是同一个 Pydantic 模型**，永远不会对不上。

### 设计 4：写入带来源标记

所有写操作经 `updated_by` 记录 `'mcp'`。

**三个月后能查出「这笔是 AI 改的」。** 这是 AI 有权限修改数据时最低限度应该有的交代。

---

## 三、安全边界总表

| 数据 | AI 能做什么 | 谁在管 |
|---|---|---|
| `transactions_raw` | 只能通过 `import_bill` 插入新行 | `importer/`，无任何 UPDATE 路径 |
| `tx_overrides` | 通过 `set_override` 增改删 | `overrides.py`，三道 403 |
| 查询 | 全部只读工具 | `reports/` |

```
┌─────────────────────────────────────────┐
│  AI 想改原始数据？                        │
│                                         │
│  ① MCP schema 里没有原始字段名 → 传不进来  │
│  ② overrides.py 黑名单再挡一次             │
│  ③ 代码里根本没有那条路径 ← 物理事实       │
└─────────────────────────────────────────┘
```

**第 3 条最硬。** 前两条可以被绕过，第三条不能——因为它不是纪律，是代码结构。

---

## 四、一个容易踩的坑

**不要在 `backend/mcp/server.py` 里直接 `conn.execute(...)`。**

```
❌ 在 backend/mcp/server.py 里直接 conn.execute("UPDATE ...")
✅ 在 backend/mcp/server.py 里调 overrides.set_override(...)
```

前者绕过了唯一的 403 校验点。**这不是风格问题，是把整个项目的核心约束作废。**

代码审查时看到 MCP 里出现 `sqlite3` 或 `conn.execute`，直接打回。

---

## 五、状态

**✅ 后端已实现**（`backend/mcp/server.py`）。11 个工具全部就位，`--selftest` 覆盖：
注册表完整、防线①（协议 schema 无原始字段）、读闭环、写闭环（临时库验证
`updated_by="mcp"` 落库）、防线②（传 `amount` 被 extra=forbid 拒绝）、404 语义。
stdio 真实客户端会话实测通过。待做：Claude Desktop 等真实客户端接入实测。

实现要点见 `1-3_MCP接入.md`。

---

## 延伸

`02_后端.md` · `1-3_MCP接入.md`
