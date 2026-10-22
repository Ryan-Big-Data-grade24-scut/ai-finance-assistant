# ai-finance-assistant

AI 个人财务助手 —— 围绕用户自己的算账逻辑打造的财务工具。

---

## 跑起来

**第一次 / 换电脑：**

```powershell
.\scripts\setup.ps1
```

**以后每次：**

```powershell
.\scripts\start-all.ps1     # 前端 + 后端各开一个窗口
```

| 地址 | 是什么 |
|---|---|
| <http://localhost:5173> | 前端页面 |
| <http://localhost:8000/docs> | 接口文档，可直接 Try it out |
| <http://localhost:8000/health> | 存活探针 |

单独启动 / 出问题：

```powershell
.\scripts\start-backend.ps1     # 只起后端 (8000)
.\scripts\start-frontend.ps1    # 只起前端 (5173)
.\scripts\check.ps1             # 自检：缺什么、哪个服务没起来
```

### MCP 接入（给 AI 客户端用）

`backend/mcp/server.py`（11 个工具，只读 9 + 可写 2）以 stdio 方式运行。自检：

```powershell
.venv\Scripts\python.exe -m backend.mcp.server --selftest
```

Claude Desktop 等客户端的配置（把 `<项目路径>` 换成实际路径）：

```json
{
  "mcpServers": {
    "ai-finance-assistant": {
      "command": "<项目路径>/.venv/Scripts/python.exe",
      "args": ["-m", "backend.mcp.server"],
      "cwd": "<项目路径>"
    }
  }
}
```

设计边界见 `docs/设计/第一轮/04_MCP.md`。红线：MCP 层只转发，`backend/mcp/` 里出现 `conn.execute` / `sqlite3` 直接打回。

### 三个坑（都是实测踩出来的，别改回去）

**1. 必须用 `.venv`。** `fastapi` 会拽着 `starlette` 一起装。直接 `pip install` 到全局 Anaconda 会把全局 starlette 降级，弄坏别的项目。所有脚本已强制走项目 venv。

**2. 前端必须在后端跑着的时候开。** 前端写相对路径 `/api/xxx`，靠 `vite.config.ts` 的 proxy 转发到 8000。**代码里不允许硬编码 `http://localhost:8000`** —— 开发能跑，部署必挂。

**3. Windows 编码。** 本项目所有 `.ps1` 必须是 **UTF-8 with BOM**（PowerShell 5.1 按 GBK 读，无 BOM 会乱码）；`requirements.txt` 同理。改脚本时别把 BOM 弄丢。

**4. 项目根不能有 `mcp/` 目录。** MCP SDK 在 PyPI 上的包名就叫 `mcp`，根级放一个 `mcp/` 目录会把 SDK 遮蔽，`import mcp` 直接炸。所以 MCP 服务器放在 `backend/mcp/`（子包不冲突），`requirements.txt` 里 `mcp` 也钉死了 `<2`（2.x 把 FastMCP 改名，现有写法会失效）。

---

## 当前进度（2026-10-16）

| 功能 | 后端 | 前端 |
|---|---|---|
| ① 跨平台账单导入 | 🟡 微信 1099 笔 + **支付宝 1804 笔**均已实测通过；其他平台未开始 | ⬜ 只有上传骨架，没接后端 |
| ② 生成 preset 报告 | ✅ 7 个接口全部跑通，数字口径逐条验证过 | ✅ **已接真实接口**（PR #1） |
| ③ MCP 接入 | ⬜ 未实现，`mcp/server.py` 不存在 | — |

### 支付宝导入已验证

2026-01 ~ 2026-09 共 2931 笔，ZIP 密码解压 + GB18030 编码探测均正常。
解析结果与账单自报汇总**笔数完全一致**（支出 1873 / 收入 163 / 中性 895）；
收入金额分毫不差。丢弃的 25 笔是 `交易关闭`+`还款失败`，本就不该入库。

> ⚠️ **支付宝导出的 zip 是加密的，解压密码由用户自行提供，不要写进任何仓库或文档。**

⚠️ 导入时另有 1102 笔被跳过：全是「滴滴青桔骑行卡抵扣」且**金额 0.00**，
撞上 schema 的 `CHECK (amount > 0)`。它们对所有汇总贡献为 0，金额不受影响，
但会让 `skipped` 计数看起来像出了 bug。

### ② 的前端已接通

账单页真调后端了：月度小结 / 趋势 / 每日日历 / 分类环形图 / 支出方式 / 近期明细，
数字与 `/api/report/*` 返回逐字段一致。注意默认月份是当月，**库里没数据时会显示 ¥0**，
点左上角 `‹` 翻到上一个有数据的月份。

---

## 必须先看懂的一件事：三条通道

**不是「请求先过 schemas.py 再过 overrides.py 才能查库」。**
`schemas.py` 不碰数据库，它只是 HTTP/MCP 边界的类型校验。

真实结构是**三条独立通道，各有唯一的门**：

| 通道 | 唯一入口 | 能做什么 |
|---|---|---|
| 写原始层 | `backend/importer/` | 只能 INSERT，**绝不 UPDATE** |
| 写修正层 | `backend/overrides.py` | UPSERT + 三道 403 防线 |
| 读 | `backend/reports/*.py` | 纯 SELECT，查 `v_tx` 视图 |

查询**不经过** `overrides.py` —— 它只管「改」，不管「读」。

### 由此推出的三条硬约束

**1. 原始凭证不可涂改。**

```
transactions_raw   导入时 INSERT，之后永不 UPDATE
tx_overrides       所有修正落这里
```

`grep -r "UPDATE transactions_raw"` 全项目零命中。想改分类，走 `overrides.py`。

**2. 前端和 AI 必须走同一条路。** 否则会出现「前端显示 A，AI 算出 B」，口径立刻分裂。

**3. 数字由后端算，措辞由 AI 决定。** MCP 拿到的数字来自 SQL，AI 改不了它，只能决定怎么措辞。

---

## 目录

```
db/schema.sql          3 表 + 2 视图 + 5 索引
backend/
  db.py                连接管理 / 事务边界 / 建表
  schemas.py           13 个接口的契约 + 字段语义 + 样例
  overrides.py         ★ 写修正的唯一入口
  main.py              FastAPI 入口（零业务逻辑）
  importer/            账单解析（wechat 已验证 / alipay 待验）
  reports/             7 个报告组件 + 待分类清单
  mcp/                 MCP 服务器（backend.mcp.server，11 个工具）
frontend/src/views/    3 页骨架（账单 / 导入 / 改单）
scripts/               setup / start-all / start-*-* / check
docs/设计/             设计文档
```

`db/bills.db` 是各自的本地账本，不入库（gitignored）。

---

## 文档

| 文档 | 什么时候看 |
|---|---|
| `docs/设计/系统设计.md` | 长期愿景与问题定义 |
| `docs/设计/第一轮/00_整体架构.md` | 三条通道为什么这么分 |
| `01_数据库.md` | 表结构与去重、事务设计 |
| `02_后端.md` | 三个核心文件各自解决什么 |
| `03_前端.md` | 骨架结构与分工边界 |
| `04_MCP.md` | MCP 层的边界与安全 |
| `1-1_导入.md` / `1-2_生成报告.md` / `1-3_MCP接入.md` | 三个功能各自的目标与进度 |

---

## 下一步

| 事项 | 谁做 |
|---|---|
| MCP 真实客户端实测（Claude Desktop 接入） | 你 |
| 支付宝导入验证 | 需要解压密码 |
| 第三方平台适配（美团 / 银行卡 / 电商） | 分到导入功能的人 |
| `frontend/components/` 9 个组件 | 队友 |
