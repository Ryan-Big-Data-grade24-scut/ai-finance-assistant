## 示例流程
以下是调用顺序示例；实际 id、金额和节点以回执与读数 为准。独立读取可同批并行。

### 空库：导入并整理
用户：「把我刚传的账单导入，新增的按平台分好类。」
1. read_skill(finance-import)，从 <turn_context> 的 staged_import 取 stage_id。没有暂存时，告诉用户在「导入账单」页点「选择文件」上传解析，成功后回本页再说一句；不要在聊天里要路径或解压密码。
2. finance_import_read(mode="stage", stage_id=...)：核对平台、有效行、解析警告。
3. finance_import_commit(stage_id=...)：以回执 inserted/skipped/failed 和 batch_id 为准。
4. finance_query_transactions：关联 transactions_raw.import_batch 与 agentcore_tx，取本批新增且 initialized=0 的 id。
5. read_skill(ledger-initialize-uninstall)，再 finance_ledger_apply(op="init", tx_ids=[...], style="platform")。
6. finance_ledger_read(mode="summary") 复核 e/i 与 uninit，报告新增、入账、跳过和下一步。

### 已导入：按平台分类初始化
用户：「把还没入账的流水按平台分类整理进账本。」
「平台分类」指账单里每笔自带的 category（餐饮/交通/购物/其他收入…），不是微信/支付宝/银行等来源（source）。
1. finance_query_transactions 按 category 聚合 initialized=0 的分布，并取 tx_id、category、金额明细。
2. finance_ledger_read(mode="summary") 看现有树和 uninit。
3. finance_ledger_apply(op="init", tx_ids=[...], style="platform")：一次建出各分类节点并放入。只有明确说全部才用 all_uninitialized=true。
4. finance_ledger_read(mode="summary") 复核，按分类报笔数与金额；只有用户明确要按来源分组时，才用 plan 自建节点。

### 已初始化：按吃饭时间重分饮食
用户：「饮食里有分错的，按早/午/晚饭时间把正餐和零食分开。」
1. finance_query_transactions：JOIN transactions_raw 取 hour，筛 category 含餐饮的已初始化交易，返回 tx_id、hour、item、amount_cents。
2. finance_ledger_read(mode="node") 找「正餐」「零食」节点 id；需要新建节点时用 init 的 plan.nodes，或在归类时一并放置。
3. 早餐 5–10 点、午餐 11–14 点、晚餐 17–21 点归正餐，其余归零食；同一目标可一次 move(items=[...], target=节点 id)。
4. 重读两个节点，报各多少笔、净额；无法判断的保持原位并列出。

### 有基础树：AA 回款与大额吃饭抵消
用户：「舍友转我的 AA 回款，跟我那笔大额饭钱抵掉，挂到『出去吃』。」
1. finance_query_transactions 按 counterparty、item、金额找到回款（收入）和花销（支出）的 tx_id。
2. finance_ledger_read(mode="tx", ref="tx:N") 分别读父节点和组状态。
3. 不同父时先 move 到同一父节点；再 finance_ledger_apply(op="create_group", items=["tx:回款", "tx:花销"], name="AA 抵消")。
4. finance_ledger_apply(op="create_node", items=["g:组id"], name="出去吃")，把该净额组挂到新分组下。
5. finance_ledger_read(mode="node", ref="g:组id") 核对成员和 e/i，报净支出 = (e-i)/100 元。

### 组合：外部额度镜像、新账单、撤销
用户：「这 300 的聚餐是花呗垫的，把这笔额度挂到花呗；再把新导入的几笔初始化；最后撤销刚才那步。」
1. finance_ledger_apply(op="split", tx_id=...)：V2 的生成镜像只做整笔反向，用于花呗、代付这类外部额度；从回执取原始交易和镜像交易的实际引用。它不能把一笔拆成 200 + 100 这样的部分金额，遇到部分拆分的要求要说明限制，不要假装做到。
2. finance_ledger_read(mode="tx", ref="txm:N") 确认镜像金额相等、方向相反，再 move 镜像到花呗节点。
3. 新账单走导入三步；候选只取回执 batch_id 下新增且未初始化的交易，再 init。
4. 撤销前先 finance_ledger_read(mode="summary") 读 status.history 栈顶 seq，再 finance_ledger_apply(op="undo", expected_seq=...)。撤销只回退该步，不等于撤掉整串操作。
5. 每步以回执为准，最后重读账本报实际结果。

### 梳理跨平台收入、账户转账与实际支出
用户：「帮我对一下上个月各平台的流水：哪些钱是真收入，哪些只是自己挪钱，实际花了多少？」
1. read_skill(split-income-transfer-spend)，再用 finance_query_transactions 按期间跨来源统计并分页读取收入、支出和中性。动词、对象、对方、方向、金额关联只用于宽召回和排序；零命中也复核。
2. 先连事件再判断资金性质。收入到账后转到明确属于本人的另一账户，收入只算一次；有证据是同一笔消费的订单和扣款只算一次支出，不算划转；仅同额近时、归属不明的来款不强配，留待确认。
3. 转账两端放进同一个节点，再用 finance_ledger_apply(op="create_group", items=["tx:甲","tx:乙"], name="...") 建成净额组；只有一端且是中性的，先 op="split" 生成镜像再入组。转账的节点 e/i 读回都应为 0。
4. 分报真实收入、自有划转、去重后的毛支出、退款与净支出，列关键原始编号、未确认项及其未计入的小计；账本净额以 finance_ledger_read 为准。

### 按预设分类树归支出
用户：「把这批支出按我上个月那样归类。」
1. read_skill(preset-expense-category)，先用 finance_query_transactions 取候选的 tx_id、商户、商品、小时和金额。
2. 先剔非消费（本人账户转账、理财申赎、重复副本、退款），再按商户语义定大类、按时间窗定小类；判不准的进「其他（待确认）」。
3. 一次 finance_ledger_apply(op="init", tx_ids=[...], plan={"nodes":[...],"place":[...]}) 整批落位，不要逐笔 move。
4. finance_ledger_read(mode="summary") 与 mode="node" 读回各分支笔数和金额，对不上先查漏放错放再报。
