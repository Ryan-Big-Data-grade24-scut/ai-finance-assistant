# ai-finance-assistant · 星云账本 V3.1

**2026-10-27 新增：可交互星云图与独立账本层。** 浏览器打开 <http://localhost:5173/star>，左侧按「年 → 月 → 日 → 交易」、右侧按「大类 → 小类 → 交易」展开同一批账；支持画布拖动/缩放、搜索交易、在分类侧拖动归位、新建分组、ADD/NET、撤销/重做、账本冻结及审计。新接口与原来的账单报告、账单导入平行运行，不覆盖旧报表口径。

**Windows 快速启动：** 首次使用可直接双击 `一键启动财务助手.cmd`，脚本会在缺少环境时自动执行安装；以后仍用同一个文件启动，使用 `一键停止财务助手.cmd` 关闭后台进程。也可以在 PowerShell 中运行 `.\scripts\setup.ps1` 和 `.\scripts\start-all.ps1`。

**星图修改 V4 测试数据。** 请将此分支克隆到新目录，再双击 `一键启动财务助手.cmd`。首次启动会从 `sample_data/star-v4-startup.sqlite3` 初始化本地账本。快照包含 110 笔已获准公开的原始流水、当前星图和已确认的 AI 分类，查看这些内容无需配置 AI。已有 `db/bills.db` 时，应用会继续使用该账本。测试星图可打开 <http://localhost:5173/star>，测试 10 月支出分类可打开 <http://localhost:5173/bill?type=expense&period=month&value=2026-10&tab=category>。

> **V3.0 Ledger 引擎完善说明（2026-10-29）**：已实现整数分金额计算、退款零腿/明确引用配对、显式外部额度 split/unsplit、外部收入镜像腿、明确订单号关联且有本人/自有账户标记的转账归零、I1–I6/根净额审计链、撤销/重做及虚构支付宝格式验收数据。部分退款不自动推断为全额退款，会发出警告并要求人工核对。旧 `TxEditView.vue` 单笔修正页面仍需后续单独完善。

> **V3.1 用户自己的 AI（2026-10-29）**：新增模型无关的 AI 分类业务层与 MCP 工具。AI 只能先提交结构化草案；用户明确确认后，建议才会作为一个原子 Ledger 修订落账。支持不确定项提问、规则提示、并发版本保护、审计记录和整批撤销。使用方法见 [docs/使用用户自己的AI.md](docs/使用用户自己的AI.md)。

**AI Intelligence V1.0**：完善微信/支付宝官方 Excel 表头识别；新增支付宝 `.xlsx` 导入；分类批次返回退款抵扣后的 `effective_amount` 和必要平台字段；特殊交易服务端过滤；新增用户规则启用/停用、修改、删除工具。

**AI Workflow V1.1（本轮）**：MCP 工具总数增至 27；新增草案列表与对话中断恢复、原草案多轮澄清、服务端规则冲突硬校验、规则关键词真实性检查、部分确认结果明示，以及 MCP 客户端配置生成和 stdio 连接冒烟测试。详见 [docs/MCP客户端接入与验证.md](docs/MCP客户端接入与验证.md)。

**Penny Agent Runtime（2026-11-05）**：新增结构化规划、工具注册与参数校验、任务/步骤状态持久化、断点恢复、证据校验和前端执行进度展示。简单只读查询仍保留低延迟路径；多目标、上下文相关或涉及修改建议的请求进入 Agent Runtime。详见 [docs/PENNY_AGENT_RUNTIME_UPGRADE.md](docs/PENNY_AGENT_RUNTIME_UPGRADE.md)。

**安全提醒：** `split` 仅对资金来源已配置为「亲情卡 / 他人代付 / 外部额度」的交易开放；只凭金额相同不会自动配对内部转账或退款。验收命令会使用临时数据库，不会修改 `db/bills.db`。

---

# ai-finance-assistant

AI 个人财务助手 —— 围绕用户自己的算账逻辑打造的财务工具。

---

## 当前 Ledger 引擎验收（V3.0）

项目根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\verify_ledger_engine.py
```

虚构支付宝格式测试账单：`sample_data/支付宝格式_引擎验收_虚构数据.csv`。验收脚本会在临时目录建库，导入 13 笔有效交易，验证退款、内部转账、部分退款警告、外部额度分裂、NET、撤销/重做和金额审计。关闭脚本后临时数据库自动删除。

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

`backend/mcp/server.py` 以 stdio 方式运行，包含报告、导入、账本和 AI 分类工作流工具（当前 27 个）。先执行服务端自检，再执行真实 stdio 客户端握手测试：

```powershell
.venv\Scripts\python.exe -m backend.mcp.server --selftest
.venv\Scripts\python.exe scripts\check_mcp_connection.py
```

用当前虚拟环境生成本机配置：

```powershell
.venv\Scripts\python.exe scripts\print_mcp_config.py --client claude
```

详细说明见 [docs/MCP客户端接入与验证.md](docs/MCP客户端接入与验证.md)。

支持本地 stdio MCP 的客户端可直接参考项目根目录的 `mcp-config.example.json`。通用格式如下：

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

分类工具固定走“查询 → 草案 → 澄清 → 用户确认 → Ledger 落账”流程。草案可跨对话恢复；服务端会强制拦截未澄清的规则冲突。MCP 层只转发；业务逻辑集中在 `backend/classification/`，账本写入仍只能经过 Ledger 引擎。

### 三个坑（都是实测踩出来的，别改回去）

**1. 必须用 `.venv`。** `fastapi` 会拽着 `starlette` 一起装。直接 `pip install` 到全局 Anaconda 会把全局 starlette 降级，弄坏别的项目。所有脚本已强制走项目 venv。

**2. 前端必须在后端跑着的时候开。** 前端写相对路径 `/api/xxx`，靠 `vite.config.ts` 的 proxy 转发到 8000。**代码里不允许硬编码 `http://localhost:8000`** —— 开发能跑，部署必挂。

**3. Windows 编码。** 本项目所有 `.ps1` 必须是 **UTF-8 with BOM**（PowerShell 5.1 按 GBK 读，无 BOM 会乱码）；`requirements.txt` 同理。改脚本时别把 BOM 弄丢。

**4. 项目根不能有 `mcp/` 目录。** MCP SDK 在 PyPI 上的包名就叫 `mcp`，根级放一个 `mcp/` 目录会把 SDK 遮蔽，`import mcp` 直接炸。所以 MCP 服务器放在 `backend/mcp/`（子包不冲突），`requirements.txt` 里 `mcp` 也钉死了 `<2`（2.x 把 FastMCP 改名，现有写法会失效）。

---

## 历史进度记录（2026-10-16）

| 功能 | 后端 | 前端 |
|---|---|---|
| ① 跨平台账单导入 | 🟡 微信 1099 笔 + **支付宝 1804 笔**均已实测通过；其他平台未开始 | ⬜ 只有上传骨架，没接后端 |
| ② 生成 preset 报告 | ✅ 7 个接口全部跑通，数字口径逐条验证过 | ✅ **已接真实接口**（PR #1） |
| ③ MCP 接入 | 当时未实现；当前 V3.1 已实现报告、账本与 AI 分类工具 | — |

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
  mcp/                 MCP 服务器（报告、账本、V3.1 AI 分类工具）
  classification/      模型无关的分类草案、确认、规则与撤销业务层
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
| MCP 真实客户端实测（选择客户端并连接） | 待本地完成 |
| 支付宝导入验证 | 需要解压密码 |
| 第三方平台适配（美团 / 银行卡 / 电商） | 分到导入功能的人 |
| `frontend/components/` 9 个组件 | 队友 |


## AI / MCP 工作流 V1.1

- `list_classification_proposals`：对话中断后找回未完成草案。
- `resolve_classification_clarification`：将用户回答写回原草案，不重复创建 proposal，也不直接落账。
- 服务端强制阻止未澄清的冲突规则建议。
- `scripts/print_mcp_config.py`：生成当前项目的 MCP 客户端配置。
- `scripts/check_mcp_connection.py`：使用 MCP Client SDK 验证 stdio 握手、工具发现和只读调用。

详细说明见 `docs/MCP客户端接入与验证.md`。

## AI Finance Web V1.0

网页原生 AI 已加入模型设置和 AI 智能导入入口：配置自己的 API Key 并明确授权必要交易字段处理后，导入账单会先进行整批分类分析，用户可以在导入预览中批准新分类、勾选交易确认，并通过批次对话校准建议。只有命中用户明确启用的规则且开启自动规则授权时才会自动落账。详见 `docs/验收/AI-Finance-Web-V1.0-验收说明.md` 和 `docs/使用/网页AI智能导入.md`。
