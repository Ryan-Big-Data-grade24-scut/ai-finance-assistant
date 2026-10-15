<script setup lang="ts">
/**
 * TxEditView —— 单笔修正页骨架
 *
 * 接口：
 *   GET   /api/tx/{id}         → TxDetailResp（原始 + 修正并排）
 *   PATCH /api/tx/{id}/override → OverrideResp
 *
 * ⚠️ 这个页面是整个项目核心卖点的入口：
 *    改完之后，报告数字会变，但原始记录一个字节没动。
 *    页面上要能同时看到「原始值」和「修正值」，让用户自己核对。
 *
 * PATCH 请求体只能有这 6 个字段，多传任何一个原始字段 → 403。
 */
const props = defineProps<{ id?: string }>()

const save = () => {
  // 单笔修正页仍保留原项目骨架；报告原型不在这里写入假数据。
}

// 接真实接口时打开：
// const save = async () => {
//   await fetch(`/api/tx/${props.id}/override`, {
//     method: 'PATCH',
//     headers: { 'Content-Type': 'application/json' },
//     body: JSON.stringify({ manual_category: form.category, note: form.note }),
//   })
// }
</script>

<template>
  <div class="tx-edit">
    <section class="card" data-component="raw" data-api="/api/tx/{id}">
      <h3>原始记录（不可修改）</h3>
      <dl>
        <dt>来源</dt><dd>{{}}</dd>
        <dt>交易时间</dt><dd></dd>
        <dt>收支</dt><dd></dd>
        <dt>金额</dt><dd></dd>
        <dt>交易对方</dt><dd></dd>
        <dt>平台原始分类</dt><dd></dd>
      </dl>
      <p class="lock">🔒 这些字段由导入器写入，系统不提供任何修改入口</p>
    </section>

    <section class="card" data-component="override" data-api="/api/tx/{id}/override">
      <h3>我的修正</h3>
      <label>
        大类
        <input type="text" placeholder="如：零食超市" />
      </label>
      <label>
        小类
        <input type="text" placeholder="如：校园超市" />
      </label>
      <label>
        <input type="checkbox" />
        豁免（不计入消费/收入统计）
      </label>
      <label>
        豁免理由
        <input type="text" placeholder="如：资金调拨，不是真实消费" />
      </label>
      <label>
        资金来源
        <select>
          <option>自有</option>
          <option>亲情卡</option>
          <option>他人代付</option>
        </select>
      </label>
      <label>
        备注
        <textarea placeholder="这笔钱到底是怎么回事" />
      </label>
      <button @click="save">保存修正</button>
    </section>
  </div>
</template>

<style scoped>
h3 { font-size: var(--font-md); margin-bottom: var(--space-3); }
dl { display: grid; grid-template-columns: 120px 1fr; gap: var(--space-2); }
dt { color: var(--color-muted); }
.lock { margin-top: var(--space-3); color: var(--color-muted); font-size: var(--font-xs); }
label { display: block; margin-bottom: var(--space-2); }
input[type="text"], select, textarea { width: 100%; padding: var(--space-2); border: 1px solid var(--color-border); border-radius: var(--radius-sm); font: inherit; }
button { padding: var(--space-2) var(--space-4); border: 0; border-radius: var(--radius); background: var(--chart-1); color: #fff; cursor: pointer; }
</style>
