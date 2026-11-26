# 答案层（Ground Truth）口径

生成器不只产出「平台看到的原始导出」，还同时产出一份同源的答案账。
两者由同一批 `Posting` 生成，答案不是事后手写的说明。

## 1. 为什么答案和平台分类是两套

真实平台按**字面关键词**和**有没有营业执照**粗分：

- 个体户小商贩只留一笔「商户收款」，真实品类消失；
- 找不到关键词就归「其他」；
- 「小米」会被当成粮油，「三只松鼠」会被当成宠物用品；
- 微信账单的「交易类型」只有「商户消费 / 扫二维码付款」，本来就没有消费分类。

所以 `scripts/synth/data.py::platform_category` 故意复现这些偏差，写进原始导出；
`scripts/synth/truth.py` 则按账务事件本身给出真实口径。错分比例写进
`ground_truth/platform_bias.csv` 与 `summary.json` 的 `platform_bias` 字段。

## 2. 支出大类

| 大类 | 判定 |
|---|---|
| 收入 | 工资、年终奖、个税退税、余额宝/理财/零钱通/理财通/银行理财收益 |
| 义务支出 | 房贷、车贷、消费贷、信用贷、信用卡分期、保险、物业费、停车位租金 |
| 刚性支出 | 水电气宽带、通勤、基础餐食、生鲜日用、医疗、教育、理发、车辆保养 |
| 弹性支出 | 咖啡、聚餐、外卖加餐、网购、服饰数码、旅行住宿、订阅、春节礼品、618/双11 |
| 中性 | 账户间划转、信用卡/花呗还款、投资本金搬运、配偶转入小荷包、退款、关闭订单 |

退款不是收入；基金/理财赎回本金按资产划转处理，避免把本金当收益。
信用卡账单与小荷包账单里的消费是**镜像行**（`flow_role=mirror`），只在平台
主流水计一次。

## 3. 周期口径

| 周期 | 典型项目 |
|---|---|
| 每日 | 早餐/午餐/晚餐、咖啡、生鲜日用、余额宝收益 |
| 每周 | 周末聚餐、朋友聚会 |
| 每月 | 工资、房贷、车贷、消费贷、信用贷、信用卡分期、还款、水电气宽带、订阅、定投 |
| 每季 | 物业费、停车位租金、学杂费、基金赎回 |
| 半年 | 车辆保养 |
| 每年 | 年终奖、个税退税、年缴保险、春节支出、618、双 11 |
| 事件 | 医疗、理发、网购、旅行、退款、关闭订单 |
| 结算 | 账户间划转、充值、信用卡/花呗还款、投资申赎 |

`ground_truth/ground_truth_periods.csv` 同时给出日/月/季/半年/年五个粒度，
`ANSWER.md` 是同一份数据的中文摘要。

## 4. 每一笔都能还原

`ground_truth/ground_truth_postings.csv` 每行带：

- `truth_id`：答案主键；
- `tx_no` / `merchant_no`：对应原始导出里的交易单号 / 商户单号；
- `source_exports`：这一笔会出现在哪些原始文件里；
- `platform_category` / `platform_mismatch`：平台分类及其是否错误；
- `expense_class` / `cadence` / `flow_role` / `mirror_of`。

`scripts/synth/truth.py::verify_ground_truth` 会逐文件读回刚写出的原始导出：

1. 有单号的文件（支付宝、微信、小荷包、银行、信用卡）按单号一对一匹配；
2. 电商订单按「平台 + 商户单号」匹配；
3. 基金、证券、银行理财、微信收益、资产快照没有单号，用整行元组做多重集匹配。

结果写入 `reconciliation.csv` / `reconciliation.json`；任何文件不是 PASS，
生成直接失败。`tests/test_synth_statements.py` 还会断言
`export_rows == matched` 且每个文件都覆盖。

## 5. 在后端跑成一本真账

答案层不只是一张 CSV，还可以走**真实导入器 + V2 账本引擎**落成两本隔离的账：

```bash
# 生成时一并跑后端账本
python -m scripts.synth --months 12 --seed 20261126 \
    --out ../.runtime/synthetic/20261126 --backend

# 或对已有数据集补跑
python -m scripts.synth.backend_ledger \
    --dataset ../.runtime/synthetic/20261126 \
    --out ../.runtime/synthetic/20261126/backend
```

产出：

- `backend/noisy.db`：只按平台分类初始化，如实保留平台的错分与「其他」；
- `backend/truth.db`：先按 `tx_no` 把答案层写进 `tx_overrides.manual_category`，
  再按「收入 / 义务支出 / 刚性支出 / 弹性支出 / 中性」两棵树初始化；
- `backend/ledger_report.json`：两本账的节点数、分类金额、平台错分率。

口径细节：

- 答案账里镜像行（信用卡/小荷包重复出现的消费）、账户间划转、投资本金搬运、
  退款、关闭订单都归「中性」并 `is_exempt=1`，所以
  `bucket_totals_counted` 与答案层的非中性金额逐类相等；
- 银行、信用卡、基金等导出目前不在导入器覆盖面内，只进答案层，不进这本后端账；
- 脚本在打开任何库之前都断言路径不是仓库里的 `db/bills.db`，
  `tests/test_synth_backend_ledger.py` 会再验一次这个保护。
