<script setup lang="ts">
/**
 * TxDetailPanel.vue —— 交易详情
 *
 * 显示这笔的 (e, i)、所在节点、是不是分裂出来的。
 * 可以分裂 / 合并 —— **能不能做由引擎说了算**，前端只负责发请求。
 */
import type { LedgerLeg, LedgerNode } from '@/types/ledger'

defineProps<{
  txId: number | null
  legs: LedgerLeg[]
  node: LedgerNode | null
  pending: boolean
}>()

const emit = defineEmits<{
  (e: 'split', txId: number): void
  (e: 'unsplit', txId: number): void
  (e: 'select-node', nodeId: string): void
}>()
</script>

<template>
  <div class="panel">
    <p v-if="!txId" class="hint">点一笔交易看详情。</p>
    <template v-else>
      <h3>τ{{ txId }}</h3>
      <ul class="legs">
        <li v-for="leg in legs" :key="leg.role">
          <span class="role" :class="leg.role">{{ leg.role === 'mirror' ? '镜像腿' : '主腿' }}</span>
          <span class="amt">支出 {{ leg.e.toFixed(2) }} / 收入 {{ leg.i.toFixed(2) }}</span>
          <a v-if="leg.node_id" href="#" @click.prevent="emit('select-node', leg.node_id)">
            {{ leg.node_id }}
          </a>
        </li>
      </ul>
      <p v-if="node" class="muted">现在在「{{ node.name }}」下</p>

      <div class="row">
        <button
          :disabled="pending || legs.some(l => l.role === 'mirror')"
          @click="emit('split', txId)"
        >分裂（＋外部额度来源）</button>
        <button
          :disabled="pending || !legs.some(l => l.role === 'mirror')"
          @click="emit('unsplit', txId)"
        >合回去</button>
      </div>
      <p class="note">分裂只对涉及外部额度（亲情卡 / 他人代付）的交易有效。</p>
    </template>
  </div>
</template>

<style scoped>
.panel { font-size: 13px; }
h3 { margin: 0 0 8px; font-size: 15px; }
.hint { color: var(--color-muted); font-size: 13px; }
.legs { list-style: none; margin: 0 0 10px; padding: 0; }
.legs li { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
.role { font-size: 10px; padding: 1px 6px; border-radius: 4px; background: #eef2f7; color: #5a6b80; }
.role.mirror { background: #fdf1e2; color: #b9761f; }
.amt { font-variant-numeric: tabular-nums; }
.legs a { margin-left: auto; font-size: 11px; color: #376fae; }
.muted { color: var(--color-muted); font-size: 12px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; }
button {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
}
button:disabled { opacity: .45; cursor: not-allowed; }
.note { color: var(--color-muted); font-size: 11px; margin-top: 8px; }
</style>
