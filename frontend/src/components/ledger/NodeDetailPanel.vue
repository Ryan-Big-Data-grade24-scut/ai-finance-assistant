<script setup lang="ts">
/**
 * NodeDetailPanel.vue —— 节点详情
 *
 * 显示 (e, i)、op、腿的列表；可以切 op、可以建子分组、可以解散。
 * ⚠️ 面板里的数字**全部来自引擎**，前端不做任何加减。
 */
import { computed, ref } from 'vue'
import type { LedgerLeg, LedgerNode } from '@/types/ledger'

const props = defineProps<{
  node: LedgerNode | null
  legs: LedgerLeg[]
  pending: boolean
  canDissolve: boolean
}>()

const emit = defineEmits<{
  (e: 'set-op', payload: { nodeId: string; op: 'ADD' | 'NET' }): void
  (e: 'dissolve', nodeId: string): void
  (e: 'select-tx', txId: number): void
  (e: 'create-child', payload: { parentId: string; name: string }): void
}>()

const newName = ref('')

const total = computed(() => (props.node ? props.node.e + props.node.i : 0))

function submitChild() {
  if (!props.node || !newName.value.trim()) return
  emit('create-child', { parentId: props.node.node_id, name: newName.value.trim() })
  newName.value = ''
}
</script>

<template>
  <div class="panel">
    <p v-if="!node" class="hint">点一个节点看详情。</p>
    <template v-else>
      <h3>{{ node.name }}</h3>
      <p class="id">{{ node.node_id }} · {{ node.kind }} · 深度 {{ node.depth }}</p>

      <div class="numbers">
        <div><span>支出</span><b class="expense">{{ node.e.toFixed(2) }}</b></div>
        <div><span>收入</span><b class="income">{{ node.i.toFixed(2) }}</b></div>
        <div><span>净值</span><b>{{ node.net.toFixed(2) }}</b></div>
      </div>

      <div class="row">
        <span class="label">聚合方式</span>
        <button
          :class="{ active: node.op === 'ADD' }"
          :disabled="pending"
          @click="emit('set-op', { nodeId: node.node_id, op: 'ADD' })"
        >＋ 叠加 ADD</button>
        <button
          :class="{ active: node.op === 'NET' }"
          :disabled="pending"
          @click="emit('set-op', { nodeId: node.node_id, op: 'NET' })"
        >≈ 净额 NET</button>
      </div>

      <div class="row">
        <span class="label">本节点持有</span>
        <span class="muted">{{ legs.length }} 条腿 · 规模 {{ total.toFixed(2) }}</span>
      </div>

      <ul class="legs">
        <li
          v-for="leg in legs"
          :key="`${leg.tx_id}-${leg.role}`"
          @click="emit('select-tx', leg.tx_id)"
        >
          <span class="tx">τ{{ leg.tx_id }}</span>
          <span class="role" :class="leg.role">{{ leg.role === 'mirror' ? '镜像' : '主腿' }}</span>
          <span class="amt">{{ leg.e.toFixed(2) }} / {{ leg.i.toFixed(2) }}</span>
        </li>
      </ul>

      <form class="row" @submit.prevent="submitChild">
        <input v-model="newName" placeholder="新分组名字" />
        <button type="submit" :disabled="pending">建子分组</button>
      </form>

      <button
        v-if="canDissolve"
        class="danger"
        :disabled="pending"
        @click="emit('dissolve', node.node_id)"
      >解散这个分组</button>
    </template>
  </div>
</template>

<style scoped>
.panel { font-size: 13px; }
h3 { margin: 0 0 4px; font-size: 15px; }
.id { margin: 0 0 10px; color: var(--color-muted); font-size: 11px; }
.hint { color: var(--color-muted); font-size: 13px; }
.numbers { display: flex; gap: 16px; margin-bottom: 12px; }
.numbers span { display: block; color: var(--color-muted); font-size: 11px; }
.numbers b { font-variant-numeric: tabular-nums; }
.expense { color: #376fae; }
.income { color: #2f9e6f; }
.row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.label { color: var(--color-muted); font-size: 12px; }
.muted { color: var(--color-muted); font-size: 12px; }
button {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
}
button.active { background: #eaf1fa; border-color: #376fae; color: #376fae; font-weight: 600; }
button.danger { color: #c0392b; border-color: #f0c4bd; }
button:disabled { opacity: .45; cursor: not-allowed; }
input { border: 1px solid var(--color-border); border-radius: 8px; padding: 5px 8px; font-size: 12px; }
.legs { list-style: none; margin: 0 0 12px; padding: 0; max-height: 190px; overflow: auto; }
.legs li {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 6px; border-radius: 6px; cursor: pointer;
}
.legs li:hover { background: #f2f6fb; }
.tx { font-weight: 600; }
.role { font-size: 10px; padding: 1px 5px; border-radius: 4px; background: #eef2f7; color: #5a6b80; }
.role.mirror { background: #fdf1e2; color: #b9761f; }
.amt { margin-left: auto; font-variant-numeric: tabular-nums; color: var(--color-muted); }
</style>
