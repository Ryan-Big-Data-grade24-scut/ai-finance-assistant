<h1 align="center">ATLAS · 个人记账系统</h1>

<p align="center">
  <code>Windows</code> · <code>FastAPI</code> · <code>Vue 3</code> · <code>SQLite</code> · <code>本地优先</code>
</p>

<p align="center">
  把微信支付和支付宝账单汇成一本账：先看钱花到哪里，再把原始流水整理成自己的分组，<br>
  最后用账本里的金额做预算。ATLAS 助手可以在同一本账上查询、核对和执行分组操作。
</p>

<p align="center">
  <b>中文</b> · <a href="#english">English</a>
</p>

---

<a id="start"></a>

## 老师先看这里 / Start here

> ### 验收用例：饮食支出梳理
>
> **用例文件：** [`docs/验收/饮食支出梳理-AI.md`](docs/验收/饮食支出梳理-AI.md)
>
> 用一份**真实来源、已脱敏**的 2025 年 10 月账单（210 笔支出），检查 ATLAS 能不能：
>
> 1. 找出所有可能是饮食的交易；
> 2. 逐笔分成 **正餐 / 零食饮品 / 待核对**，并给出理由；
> 3. 不把校园超市、个人转账、红包、退款、停车、药品等混进来；
> 4. 按列出的交易复算金额，而不是照抄平台分类。
>
> **需要：** 课程渠道单独发放的验收数据包（两份脱敏账单 + 答案表 + 确认清单）。**仓库内不含逐笔账单。**
> **时间：** 只读分类约 20–40 分钟；写账检查另加 10 分钟。
> **通过标准：** 逐笔分类与答案表一致，或差异能由商品说明解释；金额复算一致；只读阶段账本没有被改动。
>
> 不接模型也能验收：见[无 AI 操作验收](docs/验收/无AI操作验收.md)（完全虚构账单，16 笔主线 + 异常 + 批量）。需要大规模盲测数据时，见[虚构账单生成方法](synthetic-data/README.md)。

> **EN — Start here.** The acceptance case is [`docs/验收/饮食支出梳理-AI.md`](docs/验收/饮食支出梳理-AI.md). It uses a real-source, sanitized October 2025 statement (210 expenses) and checks whether ATLAS can find every possible food row, split it into *meal / snack & drink / needs review* with reasons, keep campus stores, transfers, red packets and refunds out, and recompute totals from the listed rows. The sanitized package is supplied separately through the course channel; the repository contains no per-row bills. For a no-AI check, see the [fictional-data walkthrough](docs/验收/无AI操作验收.md).

---

## 报告页的财务简报

打开当前账本的报告，选择包含今天的周、月或年，左侧会显示该周期的信纸。初次打开时正文为空。在报告页浮窗中对 ATLAS 说“根据当前账本，为本期写一份财务简报”，助手可以使用 `finance_report_brief_write` 写入。三个周期独立保存；再次撰写会更新同一封信。

预测范围为从撰写当天到期末尚未发生的收支。预计支出是义务性支出和弹性支出之和，预计收入减去义务性支出得到未来活钱，再减弹性支出得到预计结余。缺少依据的金额显示为“待估算”。信纸也可列出值得核对的支出和改善建议。

## 目录 / Contents

| 中文 | English |
|---|---|
| [1. 安装与启动](#install) | [Install & launch](#install) |
| [2. 饮食支出梳理（AI 用例）](#ai-case) | [Food-spending case](#ai-case) |
| [3. 无 AI 快速验收](#no-ai) | [No-AI smoke test](#no-ai) |
| [4. 数据边界与排错](#boundary) | [Data boundary & troubleshooting](#boundary) |
| [5. 开发与进一步验证](#dev) | [Development & verification](#dev) |

---

<a id="install"></a>

## 1. 安装与启动 / Install & launch

以下命令在 **Windows PowerShell** 中执行。先安装 Git、Python（推荐 3.12）和 Node.js LTS（含 npm），并确保命令在 `PATH` 上。本例验证环境使用 Python 3.12.7、Node.js 24.14.0、npm 11.9.0。首次安装依赖需要联网。

```powershell
git --version
python --version
node --version
npm.cmd --version

git clone https://github.com/Ryan-Big-Data-grade24-scut/ai-finance-assistant.git
cd ai-finance-assistant
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

`setup.ps1` 创建项目自己的 `.venv`，安装后端、测试和前端依赖。任何版本检查未通过，先装齐工具再运行安装脚本。`-ExecutionPolicy Bypass` 只作用于此次启动的 PowerShell 进程，不需要更改系统执行策略。安装脚本末尾也会列出开发用的 `start-all.ps1`，本指南统一使用下方的 `.cmd` 入口。

之后每次运行以下命令（也可在资源管理器双击同名文件）：

```powershell
.\一键启动财务助手.cmd
```

启动器会自动在浏览器打开 <http://127.0.0.1:5173>，页面显示「选择一本账本」。另在 PowerShell 中检查后端，预期 `status` 为 `ok`：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

- **端口占用：** 启动器不会关闭其他进程。可改用 `.\一键启动财务助手.cmd -BackendPort 18000 -FrontendPort 15173`，前端改开 `http://127.0.0.1:15173`，健康检查改用 `18000`。其他步骤不变。
- **启动失败：** 检查仓库上一级 `.runtime/` 下的 `backend-error.log` 和 `frontend-error.log`；缺依赖时重跑安装脚本。
- **停止：** 关闭启动窗口，或在窗口里按 Ctrl+C。前后端会一起停止，本地账本会保留。服务默认绑定本机。

> **EN — Install and launch.** Install Git, Python 3.12 and Node.js LTS, then run `scripts/setup.ps1` once. Double-click `一键启动财务助手.cmd` to launch the app and open <http://127.0.0.1:5173>. Use `-BackendPort 18000 -FrontendPort 15173` if the default ports are taken. Close the launcher window or press Ctrl+C to stop both local services.

---

<a id="ai-case"></a>

## 2. 饮食支出梳理（AI 用例）/ Food-spending case

**完整步骤、提示词、答案表和判分方法都在：[`docs/验收/饮食支出梳理-AI.md`](docs/验收/饮食支出梳理-AI.md)。** 本节只给出一页概览。

### 2.1 五步跑完

1. **建空账本**，先导入 `sanitized-账单-支付宝.csv`（873 笔），再导入 `sanitized-账单-微信支付.xlsx`（198 笔），合计 1,071 笔；把全部原始流水放到总账。
2. **设置模型**：OpenAI 兼容或 Anthropic；本地 Ollama 用 `http://127.0.0.1:11434/v1`，Key 可填占位值。只对脱敏包开启外发。
3. **只读分类**：打开左侧导航「✎ ATLAS」，点「新对话」，粘贴用例里的提示词；先跑 2025-10-01 ~ 10-07，再跑其余三周。提示词含一条**原样执行的 SQL**，不要改写。
4. **对照答案**：每周的笔数和金额应回到用例第 2.2 节的表；逐笔差异用 `教师答案-key-中文.csv` 的「理由」列抽查。
5. **可选写账**：把 67 笔已确认交易的 `tx_no,类别` 清单交给 ATLAS，建「饮食」及「正餐 / 零食饮品」子组，再回工作台核对树和原始流水。

### 2.2 预期结果（脱敏包口径）

| 范围 | 支出 | 正餐 | 零食饮品 | 饮食小计 | 待核对 | 非饮食 | 排除 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10-01 ~ 10-07 | 27 | 4 / 40.85 | 0 / 0.00 | **4 / 40.85** | 2 / 15.35 | 2 / 24.49 | 19 / 1,454.63 |
| 10-08 ~ 10-14 | 50 | 11 / 241.60 | 1 / 2.35 | **12 / 243.95** | 13 / 301.49 | 4 / 35.40 | 21 / 1,176.37 |
| 10-15 ~ 10-21 | 57 | 18 / 353.68 | 6 / 128.44 | **24 / 482.12** | 6 / 87.29 | 6 / 958.06 | 21 / 727.20 |
| 10-22 ~ 10-31 | 76 | 25 / 439.14 | 2 / 74.42 | **27 / 513.56** | 23 / 196.30 | 10 / 702.90 | 16 / 561.78 |
| **10 月合计** | **210** | **58 / 1,075.27** | **9 / 205.21** | **67 / 1,280.48** | **44 / 600.43** | **22 / 1,720.85** | **77 / 3,919.98** |

四类合计应等于 210 笔总额 **¥7,521.74**。金额均为脱敏包口径，不是你原始账单的金额。

> **EN — Case in one page.** Create a fresh ledger, import 873 Alipay rows then 198 WeChat rows, move all 1,071 raw rows to the root, configure a model, and run the prompts in the case document week by week. The prompt contains exact SQL. Expected weekly and monthly counts/sums are in the table; all four categories must add up to ¥7,521.74. The optional write phase creates `饮食` (67 / ¥1,280.48) with `正餐` (58 / ¥1,075.27) and `零食饮品` (9 / ¥205.21).

---

<a id="no-ai"></a>

## 3. 无 AI 快速验收 / No-AI smoke test

没有模型或数据包时，用项目生成的**完全虚构**账单检查主流程。详细步骤见[无 AI 操作验收](docs/验收/无AI操作验收.md)。

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_demo_statements.py --out ..\.runtime\demo-statements
```

新建空账本，导入 `alipay-demo.csv`（9 笔）和 `wechat-demo.xlsx`（7 笔），共 16 笔。报告应为 2026 年 10 月支出 **¥300.00 / 6 笔**、11 月支出 **¥310.00 / 8 笔**；工作台可完成「午饭」分组、移出买书、撤销/重做，以及两月饮食 **¥200.00 → ¥240.00**、下月少花 10% 得 **¥216.00** 的试算。

异常与批量用例（仅预览、重复、好坏混合、全部无效、3,000 笔去重）见详细页。

> **EN — No-AI smoke test.** Generate the fictional statements, import 16 rows, and verify the October/November report figures, the lunch group with undo/redo, and the ¥200 → ¥240 → ¥216 budget calculation. Exception and 3,000-row dedupe cases are in the detailed page.

---

<a id="boundary"></a>

## 4. 数据边界与排错 / Data boundary & troubleshooting

- **仓库只放说明、提示词、方法和聚合结果。** 真实或真实衍生的逐笔账单、答案表、`db/*.db`、`private_input/`、`*.zip`、逐笔 CSV、原始日志、截图和密钥都不入 Git。
- **`sample_data/` 只放虚构的顶层 CSV/ZIP。** 本用例不使用虚构数据冒充真实账单。
- **AI 用例只使用课程渠道的脱敏包。** 不要把个人账单替换进去；用云端模型时，必要交易字段会发送给该服务商。
- **排错：** 端口占用见第 1 节；导入失败先看预览提示的行号；模型无响应先点「测试已保存的连接」，再检查 Base URL 和模型名；只读阶段发现账本被改动，立即停止并记录，不要继续写账。

> **EN — Boundary.** Only docs, prompts, method and aggregated results are committed. Per-row bills, answer keys, databases, archives and logs stay local. `sample_data/` is fictional only. The AI case uses the sanitized course package; a cloud model receives the necessary transaction fields.

---

<a id="dev"></a>

## 5. 开发与进一步验证 / Development & verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Push-Location frontend
npm.cmd run build
Pop-Location
```

应用链路：Vue 工作台和报告经 Vite `/api` 代理访问 FastAPI；`backend/importer/` 解析账单并保存原始流水，`backend/reports/` 负责汇总，`backend/ledger2/` 负责分组、金额计算和撤销，`backend/agentcore/` 让 ATLAS 通过业务工具访问同一账本。浏览器可从 <http://127.0.0.1:8000/docs> 查看 API。各账本写在当前安装目录的 `db/`，不提交到 Git。

本页的安装、无 AI 主线和异常/批量用例已在 Windows 上用**独立的新克隆**验证：从本地代码基线 `1230812` 克隆后，重新安装依赖，从空账本完成浏览器主线、异常及批量用例，最后关闭服务；后端测试 **718 通过、1 跳过**，前端构建通过。AI 用例的本地运行记录（模型、分块、限制）见用例文档第 5 节；自动测试中的脚本化模型响应**不算**真实模型通过。

更多资料：[项目文档索引](docs/README.md) · [饮食支出梳理用例](docs/验收/饮食支出梳理-AI.md) · [无 AI 操作验收](docs/验收/无AI操作验收.md) · [账本引擎验收](docs/验收/V3.0-引擎验收说明.md) · [MCP 客户端接入与验证](docs/MCP客户端接入与验证.md) · [网页 AI 智能导入（独立工作流）](docs/使用/网页AI智能导入.md)。

> **EN — Development.** Run `pytest -q` and `npm run build`. The UI talks to FastAPI through Vite's `/api` proxy; importers, reports, ledger2 and agentcore are separate backend packages. The install and no-AI paths were verified in an isolated Windows clone (718 tests passed, 1 skipped; frontend build passed). The AI case's local run log is in its §5; scripted test models do not count as a real-model pass.

---

<h2 id="english">English</h2>

<p align="center"><a href="#start">中文</a> · <b>English</b></p>

ATLAS is a local-first personal finance app for Windows. It imports Alipay and WeChat Pay statements into one ledger, reports monthly spending, lets you build your own groups on top of the immutable raw transactions, and exposes the same ledger to an AI assistant through read/write tools.

**Acceptance case (the main thing to review):** [`docs/验收/饮食支出梳理-AI.md`](docs/验收/饮食支出梳理-AI.md). Using a real-source, sanitized October 2025 statement (210 expenses), it checks whether ATLAS can list every possible food row and classify it as *meal*, *snack & drink* or *needs review*, with a reason per row, while keeping campus stores, transfers, red packets, refunds, parking, medicine and other non-food items out. Expected weekly/monthly counts and sums are in the case. The sanitized package is delivered separately; the repository contains no per-row bills.

**Quick start.** Install Git, Python 3.12 and Node.js LTS, then run `scripts/setup.ps1` once. Double-click `一键启动财务助手.cmd` to open <http://127.0.0.1:5173>. To use different ports, pass `-BackendPort 18000 -FrontendPort 15173`. Close the launcher window or press Ctrl+C to stop.

**No-AI check:** [`docs/验收/无AI操作验收.md`](docs/验收/无AI操作验收.md) uses fully fictional statements to verify import, reports, grouping, undo/redo, budget arithmetic, exception handling and 3,000-row dedupe.

**Data boundary:** only docs, prompts, method and aggregated results are committed. Personal bills, databases, archives, per-row CSVs, logs and keys never enter Git. `sample_data/` is fictional. The AI case must use the sanitized package only.
