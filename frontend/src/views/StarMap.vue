<script setup lang="ts">
/**
 * StarMap.vue —— 星图账本页
 *
 * 布局：
 *   顶栏：BookPicker（选账/建账） + LedgerStatusBar（引擎给的数）
 *   主体：StarCanvas（径向树） + 右侧详情面板
 *
 * ⚠️ **前端不算钱。** 所有数字来自引擎，前端只负责显示和发手势。
 * ⚠️ **前端不预判合法性。** 非法手势照样发出去，让引擎用人话拒绝。
 */
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createLedgerBook } from '@/api/ledger'
import { useLedgerStore } from '@/stores/ledger'
import BookPicker from '@/components/ledger/BookPicker.vue'
import LedgerStatusBar from '@/components/ledger/LedgerStatusBar.vue'
import StarCanvas from '@/components/ledger/StarCanvas.vue'
import NodeDetailPanel from '@/components/ledger/NodeDetailPanel.vue'
import TxDetailPanel from '@/components/ledger/TxDetailPanel.vue'
import RejectToast from '@/components/ledger/RejectToast.vue'

const store = useLedgerStore()
const route = useRoute()
const router = useRouter()

const selectedNode = computed(() =>
  store.selectedNodeId ? store.nodeById(store.selectedNodeId) ?? null : null,
)

const selectedLegs = computed(() =>
  store.tree?.legs.filter(l => l.tx_id === store.selectedTxId) ?? [],
)

const selectedTxNode = computed(() => {
  const leg = selectedLegs.value.find(l => l.role === 'primary')
  return leg ? store.nodeById(leg.node_id) ?? null : null
})

function syncRoute() {
  const q: Record<string, string> = {}
  if (store.currentBookId) q.book_id = store.currentBookId
  if (store.focusNodeId) q.focus_node_id = store.focusNodeId
  router.replace({ path: '/star', query: q })
}

onMounted(async () => {
  const bookId = route.query.book_id as string | undefined
  const focus = route.query.focus_node_id as string | undefined
  if (focus) store.focusNodeId = focus
  await store.loadBooks()
  if (bookId && store.books.some(b => b.book_id === bookId)) {
    await store.selectBook(bookId)
  } else if (store.currentBookId) {
    await store.refresh()
  }
})

watch(
  () => store.currentBookId,
  () => syncRoute(),
)

function onSelectBook(bookId: string) {
  store.selectBook(bookId)
}

async function onCreateBook(payload: { name: string; dateFrom: string; dateTo: string }) {
  try {
    const tree = await createLedgerBook(payload.name, payload.dateFrom, payload.dateTo)
    await store.loadBooks()
    store.currentBookId = tree.book_id
    await store.refresh()
  } catch (e) {
    store.lastError = (e as Error).message
  }
}

function onSelectNode(nodeId: string) {
  store.selectedNodeId = nodeId
  store.selectedTxId = null
}

function onSelectTx(txId: number) {
  store.selectedTxId = txId
  store.selectedNodeId = ''
}

/** 拖 τ 到某个节点上 = 改归属 */
async function onReassign(payload: { txId: number; toNodeId: string }) {
  await store.applyOp(
    { op: 'reassign', tx_id: payload.txId, to_node_id: payload.toNodeId },
    payload.toNodeId,
  )
}

/** 拖 τ 到 τ 上 = 把两笔放进一个净额组（+₂ 手势） */
async function onGroup(payload: { txIds: number[] }) {
  const root = store.tree?.root_id
  if (!root) return
  await store.applyOp(
    {
      op: 'create_node',
      parent_id: root,
      name: '净额组',
      op_value: 'NET',
      tx_ids: payload.txIds,
    },
  )
}
</script>

<template>
  <div class="star-page">
    <header class="top">
      <BookPicker
        :books="store.books"
        :current-book-id="store.currentBookId"
        :pending="store.pending"
        @select="onSelectBook"
        @create="onCreateBook"
      />
      <LedgerStatusBar
        :status="store.status"
        :pending="store.pending"
        @undo="store.undo()"
        @redo="store.redo()"
        @reset="store.resetToM0()"
      />
    </header>

    <p class="caveat">
      账本口径：中性交易默认记支出（余额宝派息等标注「收益发放」的算收入）；
      账户搬运与退款归零。账本的数字和「账单报告」不是一回事，不要相加。
    </p>

    <div class="body">
      <section class="canvas-wrap">
        <StarCanvas
          :tree="store.tree"
          :selected-node-id="store.selectedNodeId"
          :selected-tx-id="store.selectedTxId"
          :shake-node-id="store.shakeNodeId"
          @select-node="onSelectNode"
          @select-tx="onSelectTx"
          @reassign="onReassign"
          @group="onGroup"
        />
      </section>

      <aside class="side">
        <NodeDetailPanel
          v-if="!store.selectedTxId"
          :node="selectedNode"
          :legs="store.selectedNodeId ? store.legsOf(store.selectedNodeId) : []"
          :pending="store.pending"
          :can-dissolve="!!selectedNode && selectedNode.node_id !== store.tree?.root_id"
          @set-op="store.applyOp({ op: 'set_op', node_id: $event.nodeId, op_value: $event.op })"
          @dissolve="store.applyOp({ op: 'dissolve_node', node_id: $event })"
          @select-tx="onSelectTx"
          @create-child="
            store.applyOp({
              op: 'create_node',
              parent_id: $event.parentId,
              name: $event.name,
              op_value: 'ADD',
              tx_ids: [],
            })
          "
        />
        <TxDetailPanel
          v-else
          :tx-id="store.selectedTxId"
          :legs="selectedLegs"
          :node="selectedTxNode"
          :pending="store.pending"
          @split="store.applyOp({ op: 'split', tx_id: $event }, store.selectedTxId)"
          @unsplit="store.applyOp({ op: 'unsplit', tx_id: $event }, store.selectedTxId)"
          @select-node="onSelectNode"
        />
      </aside>
    </div>

    <RejectToast :message="store.lastError" :visible="!!store.lastError" />
  </div>
</template>

<style scoped>
.star-page { display: flex; flex-direction: column; gap: 12px; }
.top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
  background: rgba(255, 255, 255, .92);
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 12px 16px;
}
.caveat {
  margin: 0;
  color: var(--color-muted);
  font-size: 12px;
  line-height: 1.6;
}
.body { display: flex; gap: 16px; align-items: stretch; }
.canvas-wrap {
  flex: 1;
  min-width: 0;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 8px;
}
.side {
  width: 300px;
  flex: none;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 14px;
}
@media (max-width: 900px) {
  .body { flex-direction: column; }
  .side { width: auto; }
}
</style>
