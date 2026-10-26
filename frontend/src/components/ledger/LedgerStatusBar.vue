<script setup lang="ts">
/**
 * LedgerStatusBar.vue —— 状态条
 *
 * 显示**引擎给的**三个数 + 不变量通过数 + 未归位数 + 撤销/重做。
 *
 * ⚠️ 口径必须标出来：账本的口径和第一轮报表**不是一回事**，
 *    中性交易默认记支出 —— 用户得看得见这个口径。
 */
import type { LedgerStatus } from '@/types/ledger'

defineProps<{
  status: LedgerStatus | null
  pending: boolean
}>()

const emit = defineEmits<{
  (e: 'undo'): void
  (e: 'redo'): void
  (e: 'reset'): void
}>()

function money(n: number): string {
  return n.toFixed(2)
}
</script>

<template>
  <div class="status-bar">
    <template v-if="status">
      <div class="metric">
        <span class="label">支出</span>
        <b class="expense">{{ money(status.root_e) }}</b>
      </div>
      <div class="metric">
        <span class="label">收入</span>
        <b class="income">{{ money(status.root_i) }}</b>
      </div>
      <div class="metric">
        <span class="label">净值</span>
        <b>{{ money(status.root_net) }}</b>
      </div>
      <div class="metric">
        <span class="label">已归位</span>
        <b>{{ status.placed_tx }} / {{ status.total_tx }}</b>
        <small v-if="status.unplaced_tx">待归 {{ status.unplaced_tx }}</small>
      </div>
      <div class="metric">
        <span class="label">不变量</span>
        <b :class="status.invariants_ok ? 'ok' : 'bad'">
          {{ status.invariants.filter(i => i.ok).length }} / {{ status.invariants.length }}
        </b>
      </div>
      <div class="actions">
        <button :disabled="!status.can_undo || pending" @click="emit('undo')">撤销</button>
        <button :disabled="!status.can_redo || pending" @click="emit('redo')">重做</button>
        <button :disabled="pending" @click="emit('reset')">回到 M₀</button>
      </div>
    </template>
    <p v-else class="hint">选一本账</p>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex;
  align-items: center;
  gap: 22px;
  flex-wrap: wrap;
}
.metric { display: flex; align-items: baseline; gap: 6px; }
.metric .label { color: var(--color-muted); font-size: 12px; }
.metric b { font-size: 16px; font-variant-numeric: tabular-nums; }
.metric small { color: var(--color-muted); font-size: 11px; }
.expense { color: #376fae; }
.income { color: #2f9e6f; }
.ok { color: #2f9e6f; }
.bad { color: #c0392b; }
.actions { display: flex; gap: 6px; margin-left: auto; }
.actions button {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
}
.actions button:disabled { opacity: .45; cursor: not-allowed; }
.hint { color: var(--color-muted); font-size: 12px; }
</style>
