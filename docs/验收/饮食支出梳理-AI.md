<h1 align="center">验收用例：饮食支出梳理</h1>

<p align="center">
  <b>中文</b> · <a href="#english">English</a>
</p>

<p align="center">
  用一份真实来源、已脱敏的月度账单，检查 ATLAS 能不能把“吃饭”和“不是吃饭”分开，<br>
  并把“看不出来”的交易留在待核对，而不是猜。
</p>

> **老师先看这里**
>
> - **用例位置：** 本文件。仓库首页第一屏也有直达链接。
> - **需要什么：** 课程渠道单独发放的验收数据包（两份脱敏账单 + 答案表），以及一个可用的 OpenAI 兼容或 Anthropic 模型。
> - **要多久：** 只读分类约 20–40 分钟；写账检查另加 10 分钟。
> - **怎么算通过：** 逐笔分类与答案表一致，或差异能被账单原文解释；金额按列出的交易复算一致；只读阶段账本没有被改动。详见[怎么判分](#grading)。

---

## 0. 这个用例在测什么 / What it tests

这不是“让 AI 说一段理财建议”，而是一次**可逐笔核对**的分类任务：

1. 从 2025 年 10 月的 210 笔支出中，找出所有**可能是饮食**的交易；
2. 分成 **正餐 / 零食饮品 / 待核对** 三类，逐笔给出 id、日期、对方、商品说明、金额和一句理由；
3. 明确非饮食、资金流转、退款、收入、还款不混进来；
4. 最后按列出的交易复算笔数和金额，而不是照抄账单自带的“交易分类”。

账单自带的分类在这份数据里有陷阱：食堂消费可能被平台标成“教育培训”；团购券、兑换码可能是正经一顿饭；校园超市、个人转账、AA 回款信息不足，只能进“待核对”。**能不能识别这些陷阱，就是这个用例要看的。**

> **EN — What this case tests.** Given 210 expense rows from October 2025, ATLAS must find every row that could be food, split it into *meal / snack & drink / needs review*, list each row with a reason, and recompute the totals from those rows. The platform's own category is unreliable (a canteen meal may be labelled “education”), vouchers can be real meals, and campus stores or personal transfers must stay in *needs review* rather than being guessed. The case is passed by row-level agreement with the answer key, not by a plausible-sounding summary.

---

## 1. 准备 / Setup

### 1.1 验收数据包

数据包由课程渠道单独提供，**不在本仓库内**。仓库只保存本说明、提示词、判分方法和聚合后的预期结果；逐笔账单、答案表和确认清单都在包内，不进入 Git。

| 文件 | 用途 | 说明 |
|---|---|---|
| `sanitized-账单-支付宝.csv` | 导入支付宝流水 | UTF-8 BOM，可直接被现有导入器读取 |
| `sanitized-账单-微信支付.xlsx` | 导入微信流水 | 与支付宝合并成一本账 |
| `教师答案-key-filtered.csv` | 逐笔答案 | 210 行，列为 `id,tx_no,time,party,item,amount,label,reason` |
| `教师答案-key-中文.csv` | 同上，中文类别 | 列为 `id,交易号,时间,交易对方,商品说明,金额,类别,理由`；`类别` 为 正餐/零食饮品/待核对/非饮食/排除 |
| `确认交易-分组.csv` | 写账阶段用 | 67 笔已确认饮食交易及类别（`tx_no,类别`） |
| `确认交易-tx_no.txt` | 写账阶段备用 | 67 个 `tx_no`，每行一个 |

数据已经脱敏：日期整体平移、金额按商户做受控缩放、姓名/账户/订单号/真实商户名替换为受控短语。**因此本文所有金额都是脱敏包的金额，不是你原始账单的金额。**

> **EN — Data package.** The two sanitized statements and the answer key are supplied separately through the course channel and are not stored in this repository. Dates are shifted, amounts are scaled per merchant, and identifiers are replaced, so every number below belongs to the sanitized package, not to the original bill.

### 1.2 新建账本并导入

按仓库首页的[安装与启动](../../README.md#install)完成环境准备，打开 <http://127.0.0.1:5173>。

1. 在首页点「新建账本」，命名 `饮食用例`，点「新建并打开」。**不要**在已有个人账本上运行本用例。
2. 打开「导入账单」，选 `sanitized-账单-支付宝.csv`，点「预览账单」。核对识别平台为支付宝、有效交易 **873 笔**，再点「确认导入数据库」。
3. 同一步骤导入 `sanitized-账单-微信支付.xlsx`，识别为微信支付，新增 **198 笔**。
4. 两份合计 **1,071 笔**（其中 2025 年 10 月支出 210 笔）。打开「账本工作台」→「全部」→「将原始流水导入账本」，全选，选「全部放到总账」。这一步只是把已有流水放进工作台，不会新增原始交易。

> **注意：** 导入顺序固定为“先支付宝、后微信”。这样交易 `id` 与答案包的 `id` 列一致；如果顺序反了，分类结论仍然成立，但按 `id` 对照会错位，请改用 `tx_no` 对照。答案表第一列 `id` 就是查询结果里的交易 id，`tx_no` 是账单原始交易号。

### 1.3 设置模型

打开左侧导航「✎ ATLAS」，在右侧「AI 模型」卡片点「去设置 AI 模型」：

- **服务商协议：** OpenAI 兼容（Ollama、vLLM、云端 OpenAI 兼容服务均可）或 Anthropic。
- **API Base URL / 模型名称 / API Key：** 按服务商填写。用本地 Ollama 时，Base URL 填 `http://127.0.0.1:11434/v1`，模型填已拉取的模型名（例如 `qwen3.5:9b`），API Key 可填任意占位值。
- 保存后点「测试已保存的连接」。
- 打开「允许将必要交易字段发送到模型接口」。若用云端服务，交易字段会发送给该服务商；**只使用脱敏包**，不要导入原始账单。

> **EN — Model setup.** Create a fresh ledger, import the Alipay file first (873 rows) and the WeChat file second (198 rows), move all 1,071 raw rows to the root group, then configure an OpenAI-compatible or Anthropic model. For local Ollama use `http://127.0.0.1:11434/v1` and a placeholder key. Only the sanitized package may be sent to a cloud provider.

---

## 2. 只读阶段：先分类，不改账 / Read-only phase

### 2.1 提示词（按周执行）

在左侧导航打开「✎ ATLAS」，点「新对话」，把下面整段粘贴进去。**第一次先只跑 10 月 1–7 日**，确认输出格式和账本都没变，再跑其余三周。

```text
请只做分析，先不要修改账本。范围：2025 年 10 月 1 日到 10 月 7 日（含）的全部支出。

第一步必须原样执行这条查询，不要改写 SQL：
SELECT id, date, amount, direction, counterparty, item FROM transactions_raw WHERE direction = '支出' AND date >= '2025-10-01' AND date <= '2025-10-07' ORDER BY date, id

第二步：从查询结果中找出所有“可能是饮食”的支出，只分三类：
- 正餐：可当作一顿饭的消费（食堂、面/饭/粉、烧烤、火锅、快餐等）；团购券、兑换码按商品实际品类归入，不因“券”字归为待核对。
- 零食饮品：烘焙、水果、咖啡、奶茶、冰淇淋、饮料等。
- 待核对：可能是饮食，但现有信息不足，例如超市/便利店/校园超市没有商品明细、个人转账/AA、无法辨识的扫码商户。

其余支出（明确非饮食，以及资金流转、红包、交通卡/电话卡储值充值、退款、收入、还款）不必逐笔列出，只在最后报一行“其他：N 笔，合计 X 元”，其中排除类单独报笔数。

要求：
1. 正餐、零食饮品、待核对必须逐笔列出，每行写：id、日期、对方、商品说明、金额（元）、类别、一句理由。
2. 不得遗漏任何可能是饮食的支出；也不要把明确非饮食（停车、交通、游泳馆、药品、电费、会员、打印、购物等）放进待核对。
3. 账单自带的“交易分类”不可靠，食堂消费也可能被标成“教育培训”，必须看商品说明和交易对方。
4. 不要凭金额、时间或商户名猜；没有证据就放“待核对”。
5. 最后给出三类各自笔数与合计（元），以及其他类合计；单独再列一遍所有“待核对”项。
6. 不要初始化账本、不要建组、不要移动任何交易。
7. 汇总金额必须按查询结果逐笔复算，不要估算；最终只输出结论和表格，不要在正文中反复自我纠正或粘贴草稿。
8. 金额必须与查询结果中的 amount 完全一致；不要换算、不要估算、不要根据“标价/折扣”改写金额。
9. 只使用查询结果中的交易；不要引入范围外或结果中不存在的 id、商户、金额。
```

跑完第一周后，把提示词里的三个日期和 SQL 里的两个边界改成下一周：

| 轮次 | 日期范围 | SQL 边界 |
|---|---|---|
| 第 1 周 | 10 月 1–7 日 | `'2025-10-01'` … `'2025-10-07'` |
| 第 2 周 | 10 月 8–14 日 | `'2025-10-08'` … `'2025-10-14'` |
| 第 3 周 | 10 月 15–21 日 | `'2025-10-15'` … `'2025-10-21'` |
| 第 4 周 | 10 月 22–31 日 | `'2025-10-22'` … `'2025-10-31'` |

> **模型输出被截断或漏行怎么办？** 这是模型能力问题，不是案例问题。把范围缩小到 2–4 天，用同一段提示词重跑；文档末尾记录了你实际用的模型和是否分块，验收时如实注明即可。**不要**为了让结果好看而手工补行。

### 2.2 预期结果（脱敏包口径）

每周的正确答案如下。笔数为支出笔数；金额单位为元。

| 范围 | 支出总笔数 | 正餐 | 零食饮品 | 饮食小计 | 待核对 | 非饮食 | 排除 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10-01 ~ 10-07 | 27 | 4 / 40.85 | 0 / 0.00 | **4 / 40.85** | 2 / 15.35 | 2 / 24.49 | 19 / 1,454.63 |
| 10-08 ~ 10-14 | 50 | 11 / 241.60 | 1 / 2.35 | **12 / 243.95** | 13 / 301.49 | 4 / 35.40 | 21 / 1,176.37 |
| 10-15 ~ 10-21 | 57 | 18 / 353.68 | 6 / 128.44 | **24 / 482.12** | 6 / 87.29 | 6 / 958.06 | 21 / 727.20 |
| 10-22 ~ 10-31 | 76 | 25 / 439.14 | 2 / 74.42 | **27 / 513.56** | 23 / 196.30 | 10 / 702.90 | 16 / 561.78 |
| **10 月合计** | **210** | **58 / 1,075.27** | **9 / 205.21** | **67 / 1,280.48** | **44 / 600.43** | **22 / 1,720.85** | **77 / 3,919.98** |

校验：`1,280.48 + 600.43 + 1,720.85 + 3,919.98 = 7,521.74`，等于 210 笔支出的总额。

如果你把一周又拆成 2–4 天的小块运行，把各块的笔数和金额相加，仍应回到上表的周数字和月数字；不要只对单块判分。

> **EN — Expected results.** The table gives the correct counts and sums per week and for the month. “Excluded” means money movement, red packets, transit-card top-ups, refunds, income and repayments; “non-food” means clearly non-food spending. The four category sums must add up to the 210-row total, ¥7,521.74.

<a id="grading"></a>

### 2.3 怎么判分 / How to grade

按顺序检查，前四项是硬指标：

| # | 检查 | 通过标准 |
|---|---|---|
| 1 | **覆盖** | 67 笔饮食全部出现在正餐/零食饮品；44 笔待核对全部出现在待核对；22 笔非饮食没有被放进待核对；77 笔排除没有被算成饮食或普通消费。四类合计 210 笔 |
| 2 | **类别** | 58 笔正餐、9 笔零食饮品与答案表一致；边界差异能由商品说明解释 |
| 3 | **金额** | 三类的笔数和合计与上表一致；总计 7,521.74 |
| 4 | **只读** | 工作台仍是 1,071 笔，原始流水 1,071 笔，没有新建分组或移动交易 |
| 5 | **理由** | 每笔待核对都说明了“缺什么信息”，没有用金额、时间或商户名猜 |
| 6 | **边界** | 退款、AA 回款、红包、小荷包/余额宝、交通卡充值、还款没有混进普通毛支出 |

**判定建议：**

- **通过：** 硬指标 1–4 全过；个别边界项与答案表不同，但理由成立（例如把“轻食”判成正餐还是待核对），在验收记录里注明即可。
- **部分通过：** 覆盖或金额有明显缺口，但缺口可定位、可解释；把缺口逐条列出，不要求模型重跑到“全对”。
- **不通过：** 把校园超市、个人转账、红包成批当成饮食；或只复述平台分类；或只读阶段改了账本。

答案表里的 `label` 是英文代码：`meal`=正餐，`snack`=零食饮品，`pending`=待核对，`non_food`=非饮食，`excluded`=排除。`reason` 列写了每笔的判定依据，可直接用于抽查。

### 2.4 常见错误对照

| 现象 | 为什么错 | 正确做法 |
|---|---|---|
| 把食堂档口按平台分类“教育培训”排除 | 平台分类不可靠 | 看 `item` 里的“食堂/饭/面/轻食”等商品说明 |
| 把“兑换码/团购券”一律放待核对或排除 | 券只是支付形式，不是品类 | 按券对应商品归入正餐或零食 |
| 把校园超市、便利店直接算零食 | 没有商品明细 | 放待核对，并说明缺商品名 |
| 把个人转账、AA 回款算饮食 | 不是消费凭证 | 放待核对；若确认是还款/转账则排除 |
| 把红包、小荷包、余额宝转入转出算成饮食或普通消费 | 资金流转 | 排除，不计入饮食或普通消费 |
| 把停车、游泳、药品、电费、会员、打印放待核对 | 已能确定不是饮食 | 归非饮食，不占用待核对名额 |
| 用平台“支出分类”的合计当饮食合计 | 分类口径不同 | 只汇总自己逐笔列出的交易 |

> **EN — Grading.** Check coverage, labels, sums, and that the read-only phase did not touch the ledger. A defensible borderline call is acceptable if recorded; systematic guessing of campus stores, transfers or red packets is a failure. The answer key's `label` column uses `meal / snack / pending / non_food / excluded`.

---

## 3. 写账阶段（可选）/ Write phase

只读分类通过后，再检查 ATLAS 能不能把**已确认的交易**正确写进账本。这一步不重新分类，避免把分类错误和写账错误混在一起。

1. 打开数据包里的 `确认交易-分组.csv`，复制 67 行 `tx_no,类别`。
2. 在同一个 ATLAS 对话里追加：

```text
只执行账本操作，不要重新分类，不要改动其他交易。

下面是我已逐笔确认的 67 笔饮食支出（tx_no,类别）：
<在这里粘贴 确认交易-分组.csv 的内容>

请：
1. 先在 transactions_raw 中查出这些 tx_no 对应的 id；
2. 用这些交易创建分组「饮食」，备注「2025 年 10 月饮食，逐笔确认」；
3. 在「饮食」下创建「正餐」和「零食饮品」两个子组，按清单里的“类别”列放入；正餐应为 58 笔，零食饮品应为 9 笔；
4. 待核对、非饮食、排除的交易一律不要放入；
5. 执行后返回每个组的笔数和合计，并说明原始流水是否仍是 1,071 笔。
```

3. 回「账本工作台」核对：

| 组 | 笔数 | 合计 |
|---|---:|---:|
| 饮食 | 67 | ¥1,280.48 |
| ├ 正餐 | 58 | ¥1,075.27 |
| └ 零食饮品 | 9 | ¥205.21 |

- 总账直接挂的交易应为 **1,004 笔**（1,071 − 67）。
- 原始流水仍是 **1,071 笔**，金额不变。
- 非饮食和待核对的交易不应出现在「饮食」下。
- 若写错，用工作台的「撤销」逐步回退；**不要**假设一次撤销能回退整批操作。
- 可选：问 ATLAS“如果 11 月饮食比 10 月少 10%，上限是多少？”正确结果是 `1,280.48 × 0.90 = ¥1,152.43`。这是基于本范围开销的试算，不是自动记入的预算。

> **EN — Optional write phase.** After the read-only pass, paste the 67 `tx_no,category` rows and ask ATLAS to resolve their ids and create `饮食` (67 rows), with `正餐` (58) and `零食饮品` (9) beneath it. Verify the tree sums, root direct count 1,004, and raw count 1,071 unchanged. Do not let the model re-classify at this stage.

---

## 4. 数据边界 / Data boundary

| 内容 | 在哪里 | 是否进 Git |
|---|---|---|
| 本说明、提示词、判分方法、聚合预期结果 | 本仓库 | 是 |
| 两份脱敏账单、答案表、确认清单 | 课程渠道数据包 | **否** |
| 原始账单、解压密码、脱敏映射、原始日志 | 本地 | **否** |
| 任何 `db/*.db`、`private_input/`、`*.zip`、逐笔 CSV | 本地 | **否** |

仓库的 `sample_data/` 只放**虚构**顶层 CSV/ZIP；本用例不使用虚构数据冒充真实账单。若没有收到数据包，请不要用个人账单替代，也不要从网络找一份“看起来像”的账单——本用例的答案表只对这份脱敏包有效。

> **EN — Data boundary.** The repository contains only this document, prompts, method and aggregated expected results. The sanitized statements, answer key and confirmation list are delivered separately and are never committed. Original bills, passwords, mapping files and logs stay local.

---

## 5. 我们实际跑过 / What we ran

这一节记录项目组在本地做过的真实模型运行，便于老师判断“提示词是否可执行”，而不是只看到一份设计稿。**结论先说：本机 9B 模型能走通“查询 → 逐笔分类”的路径，但不足以在整月上稳定给出可直接判通过的结果；验收请用能力更强的模型，并始终以答案表复算为准。**

- **模型：** `qwen3.5:9b`，通过本机 Ollama 运行，接口只绑定 `127.0.0.1`，未回退云端。
- **数据：** 本文所述脱敏包，导入后 1,071 笔原始流水，10 月支出 210 笔。
- **方式：** 只读阶段，按 3–7 天分块执行同一段提示词；每块单独查询 `transactions_raw`，逐笔列表。工具调用本身正常（代理日志确认请求携带 8 个工具，模型发起了 `finance_query_transactions`）。

| 运行 | 范围 | 支出 | 饮食列全 | 待核对列全 | 实际表现 |
|---|---|---:|---|---|---|
| 第 1 周 | 10-01 ~ 10-07 | 27 | 4 / 4 | 2 / 2 | 逐笔 id、类别全对；但自报“其他”合计少 ¥65.00，总额随之错 |
| 第 2 周 | 10-08 ~ 10-14 | 50 | 11 / 12 | 4 / 13 | 漏 1 笔饮食、9 笔待核对，另误列 1 笔；金额串列 |
| 3 天分块 | 10-01 ~ 10-03 | 18 | 3 / 3 | 0 / 1 | 漏校园超市；把小荷包误列待核对后又自我否定；546 金额被改写 |
| 第 3 周 | 10-15 ~ 10-21 | 57 | — | — | `OUTPUT_TRUNCATED`，模型思考超长，未完成 |
| 第 4 周 | 10-22 ~ 10-31 | 76 | — | — | 预计同样超长，手动停止 |

**已知限制（如实说明）：**

1. 9B 级本地模型在单块超过约 30 笔时，可能输出截断、漏行或把金额串行；按 3–7 天分块可以降低概率，但不能保证。换成能力更强的云端模型时，可以按周甚至整月一次跑，但**验收标准不变**。
2. 模型自报的汇总不可信：第 1 周分类全对，但“其他”合计少 ¥65.00。这正是要求“按列出的交易复算”的原因；判分以答案表复算为准。
3. 本项目没有把模型的分类结果当成答案。答案表是人工逐笔标注、再用导入器复算金额得到的；模型只用于验证提示词和流程可执行。
4. 写账阶段的账本行为已在脱敏库副本上验证：`饮食` 67 笔 / ¥1,280.48，`正餐` 58 笔 / ¥1,075.27，`零食饮品` 9 笔 / ¥205.21，总账直接挂 1,004 笔，原始流水 1,071 笔不变；撤销/重做正常。该验证直接调用页面同一套 `ledger2` 引擎，不碰个人 `db/bills.db`。

> **EN — What we ran.** `qwen3.5:9b` on local Ollama over loopback only. Week 1 (27 rows) was row-correct: 4/4 food and 2/2 needs-review, but the model's self-reported “other” total was ¥65.00 short. Week 2 (50 rows) missed 1 food and 9 needs-review rows. A 57-row run hit `OUTPUT_TRUNCATED`; the 76-row run was stopped. The 9B model is useful for exercising the prompt and tools, not as evidence of a full-month pass; use a stronger model and grade against the key. The answer key is human-labelled and recomputed from the importer. The ledger write path was verified on a copy of the sanitized database (67 / ¥1,280.48, with 58 and 9 in the two sub-groups; root direct 1,004; raw 1,071 unchanged; undo/redo working).

---

<h2 id="english">English guide</h2>

This is a condensed English version of the same case. The Chinese sections above are authoritative for UI labels and file names.

**What it tests.** ATLAS must classify every October 2025 expense that could be food into *meal*, *snack & drink*, or *needs review*, list each row with a reason, and recompute totals from those rows. Platform categories are unreliable, vouchers can be meals, and campus stores, personal transfers and AA repayments must remain *needs review*.

**Setup.** Create a fresh ledger. Import `sanitized-账单-支付宝.csv` first (873 rows), then `sanitized-账单-微信支付.xlsx` (198 rows). Move all 1,071 raw rows to the root group. Configure an OpenAI-compatible or Anthropic model; for local Ollama use `http://127.0.0.1:11434/v1`. Only the sanitized package may be sent to a cloud provider.

**Read-only prompt.** Use the full Chinese prompt in §2.1. Run one week first (2025-10-01 to 2025-10-07), then 10-08–10-14, 10-15–10-21 and 10-22–10-31. The prompt contains the exact SQL; do not paraphrase it. If the model truncates, halve the date range and note it.

**Expected results (sanitized package).**

| Range | Expenses | Meal | Snack | Food | Needs review | Non-food | Excluded |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10-01–10-07 | 27 | 4 / 40.85 | 0 / 0.00 | 4 / 40.85 | 2 / 15.35 | 2 / 24.49 | 19 / 1,454.63 |
| 10-08–10-14 | 50 | 11 / 241.60 | 1 / 2.35 | 12 / 243.95 | 13 / 301.49 | 4 / 35.40 | 21 / 1,176.37 |
| 10-15–10-21 | 57 | 18 / 353.68 | 6 / 128.44 | 24 / 482.12 | 6 / 87.29 | 6 / 958.06 | 21 / 727.20 |
| 10-22–10-31 | 76 | 25 / 439.14 | 2 / 74.42 | 27 / 513.56 | 23 / 196.30 | 10 / 702.90 | 16 / 561.78 |
| **Month** | **210** | **58 / 1,075.27** | **9 / 205.21** | **67 / 1,280.48** | **44 / 600.43** | **22 / 1,720.85** | **77 / 3,919.98** |

The four category sums must add up to ¥7,521.74.

**Pass criteria.** (1) All 67 food rows and 44 needs-review rows are listed; the 22 non-food rows are not put in needs-review, and the 77 excluded rows are not counted as food or ordinary consumption. All four categories add up to 210 rows. (2) Labels match the key, or differences are explained by the item text. (3) Counts and sums match the table. (4) The read-only phase leaves the ledger at 1,071 raw rows with no new groups. A defensible borderline call is acceptable if recorded; systematic guessing of campus stores, transfers or red packets is a failure.

**Optional write phase.** Paste the 67 `tx_no,类别` rows from `确认交易-分组.csv`, ask ATLAS to resolve their ids and create `饮食` (67 rows, ¥1,280.48), with `正餐` (58, ¥1,075.27) and `零食饮品` (9, ¥205.21) beneath it. Verify root direct count 1,004 and raw count 1,071 unchanged. The 10% budget check is ¥1,152.43.

**Data boundary.** The repository contains only this document, prompts, method and aggregated results. The sanitized statements, answer key and confirmation list are delivered separately and must never be committed. Original bills, passwords and mapping files stay local.

**What we ran.** `qwen3.5:9b` on local Ollama over loopback, in 3–7 day chunks. It completed week 1 with all 4 food and 2 needs-review rows correct, but its self-reported “other” total was ¥65.00 short; week 2 missed 1 food and 9 needs-review rows; a 57-row run hit `OUTPUT_TRUNCATED`. A 9B model is therefore useful for exercising the prompt and tools, but not as evidence of a full-month pass. Use a stronger model for acceptance and always grade against the key. The answer key was human-labelled and recomputed from the importer, not produced by the model. The ledger write path was verified on a copy of the sanitized database: `饮食` 67 / ¥1,280.48, `正餐` 58 / ¥1,075.27, `零食饮品` 9 / ¥205.21, root direct 1,004, raw 1,071 unchanged, undo/redo working.
