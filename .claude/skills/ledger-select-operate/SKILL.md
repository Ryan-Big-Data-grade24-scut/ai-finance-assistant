---
name: ledger-select-operate
description: 按 V2 选择语义直接移动、新建节点或净额组、拆分、解散、改名与记备注
when_to_use: 用户要移动、建组、拆分、解散、改名，或问为什么动了一整组时
---

# 选择与操作

## 操作顺序

1. **读准对象。** `finance_ledger_read` 的 `summary`、`node`、`tx` 模式能确认父节点、组和金额。按 `next_offset` 读全需要的子项；需要见全体成员时不要把日期投影当全树。节点用 id，主交易用 `tx:N`，镜像用 `txm:N`，组作选择用 `g:ID`，`group_id` 参数只填 ID。重名节点用路径和 id 区分。
2. **选对操作。** 用户明确要求改时直接 `finance_ledger_apply`：
   - 移动：`move(items,target)`；从已有选择新建节点并收纳：`create_node(items,name,note?)`；在有交易可初始化时也可用 init 的 `new_node` 或 `plan` 建节点。
   - 组净额：`create_group(items,name,note?)`；批量加入已有组：`join(items,group_id)`；增减单成员：`add_member` / `remove_member`，给 `group_id,tx`。至少两成员，必须同父，不能同时在另一个组。
   - 解散选择：`dissolve(items)`；只解指定节点/组：`dissolve_node(node_id)` / `dissolve_group(group_id)`。
   - 分裂或合回：`split` / `unsplit`，用 `items` 或一个 `tx_id`。镜像不是另一条原始凭证；人工字段属于主交易。
   - 改名或备注：`update(item,name?,note?)` 支持节点、组和主交易；空备注可清除。也可用 `update_node`、`rename_node`、`rename_group`、`update_manual(tx_id,title?,note?)`。
3. **按结果推进。** 需要先移动再建组时，等移动回执成功，重读同父状态，再建组。最后读对象、成员和金额，汇报实际影响与关键引用；业务错误先修对象/参数，不盲改别的交易。

## 为什么

- **折叠避免同一对象被处理两次。** `move` / `join` / `create_group` / `create_node` 折叠选择覆盖；节点包含它的子树，移动剩余的组员可能带动整组。若「只移动这一笔」与整组语义冲突，先问是保留还是解散组。
- **展开用于对子对象施加操作。** `dissolve` / `split` / `unsplit` 会把节点展开成可执行对象。仅想解一层用单项入口，否则范围可能更大。日期只决定显示，不限制写入的树范围。
- **不同父节点不能共用净额组。** 否则同一个抵消关系会在树的两个地方计数；引擎以同父语义拒绝。节点也不能移动进自己的子树（会成环）。只在移动属于用户目标时先移动，不能为凑齐组擅自迁移其它节点。

## 虚构示例

用户明确说「把零食那笔 128 元移到聚餐，再跟那笔 300 元合组」。读到对应 `tx:88` 和 `tx:91` 后直接移动，确认 88 和 91 同父，再建组，用真实 `g:ID` 读回组金额。不能把移动成功当成建组成功。
