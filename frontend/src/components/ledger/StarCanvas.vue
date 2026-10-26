<script setup lang="ts">
/**
 * StarCanvas.vue —— 星图本体（ECharts 径向树）
 *
 * 画什么：
 *   节点 = 圆点（分组），交易腿 = 小圆点（叶子）
 *   点节点和点交易**必须一眼能分开** —— 大小 + 颜色 + 标签前缀三重区分
 *   ADD / NET 用 label 前缀 ＋ / ≈ 区分
 *
 * ⚠️ **ECharts tree series 没有内置节点拖拽。**
 *    「把 τ 拖到 τ₂ 上 = +₂」是自己实现的：
 *    chart.on('mouseover') 记住光标下的元素 → zr.on('mouseup') 判定落点。
 *
 * ⚠️ **前端只识别手势，不预判合法性。**
 *    拖出非法组合也照样发请求，让引擎返回人话理由 —— 前后端口径永远一致。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import type { LedgerTree } from '@/types/ledger'

const props = defineProps<{
  tree: LedgerTree | null
  selectedNodeId: string
  selectedTxId: number | null
  shakeNodeId: string
}>()

const emit = defineEmits<{
  (e: 'select-node', nodeId: string): void
  (e: 'select-tx', txId: number): void
  (e: 'reassign', payload: { txId: number; toNodeId: string }): void
  (e: 'group', payload: { txIds: number[] }): void
}>()

const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

/** 光标下的元素（拖拽落点判定用） */
let hoverTarget: { kind: 'node' | 'tx'; id: string; txId?: number } | null = null
/** 拖拽起点 */
let dragFrom: { kind: 'node' | 'tx'; id: string; txId?: number } | null = null

interface TreeItem {
  name: string
  value: number
  nodeId?: string
  txId?: number
  isTx?: boolean
  op?: string
  children?: TreeItem[]
  itemStyle?: Record<string, unknown>
  symbolSize?: number
}

function buildData(tree: LedgerTree): TreeItem[] {
  const nodeById = new Map(tree.nodes.map(n => [n.node_id, n]))
  const root = nodeById.get(tree.root_id)
  if (!root) return []

  const legsByNode = new Map<string, typeof tree.legs>()
  tree.legs.forEach(l => {
    const arr = legsByNode.get(l.node_id) ?? []
    arr.push(l)
    legsByNode.set(l.node_id, arr)
  })

  const build = (nodeId: string): TreeItem | null => {
    const node = nodeById.get(nodeId)
    if (!node) return null
    const magnitude = Math.abs(node.e) + Math.abs(node.i)
    const item: TreeItem = {
      name: `${node.op === 'NET' ? '≈' : '＋'}${node.name}`,
      value: magnitude,
      nodeId: node.node_id,
      op: node.op,
      symbolSize: Math.max(14, Math.min(52, 12 + Math.sqrt(magnitude) * 1.6)),
      itemStyle: {
        color: node.e > node.i ? '#376fae' : '#2f9e6f',
        borderColor: node.op === 'NET' ? '#e08a2e' : 'rgba(255,255,255,.85)',
        borderWidth: node.op === 'NET' ? 3 : 1.5,
      },
    }
    const children: TreeItem[] = []
    for (const child of tree.nodes) {
      if (child.parent_id !== node.node_id) continue
      const c = build(child.node_id)
      if (c) children.push(c)
    }
    for (const leg of legsByNode.get(node.node_id) ?? []) {
      children.push({
        name: `τ${leg.tx_id}`,
        value: Math.abs(leg.e) + Math.abs(leg.i),
        txId: leg.tx_id,
        isTx: true,
        symbolSize: 7,
        itemStyle: {
          color: leg.i > leg.e ? '#2f9e6f' : '#8fa8c4',
          borderColor: leg.role === 'mirror' ? '#e08a2e' : 'rgba(255,255,255,.9)',
          borderWidth: leg.role === 'mirror' ? 2 : 1,
        },
      })
    }
    if (children.length) item.children = children
    return item
  }

  return [build(tree.root_id)].filter(Boolean) as TreeItem[]
}

function render() {
  if (!el.value) return
  if (!chart) chart = echarts.init(el.value)
  const tree = props.tree
  if (!tree) {
    chart.clear()
    return
  }
  chart.setOption(
    {
      tooltip: { trigger: 'item', triggerOn: 'mousemove' },
      series: [
        {
          type: 'tree',
          data: buildData(tree),
          top: '4%',
          left: '6%',
          bottom: '4%',
          right: '22%',
          layout: 'radial',
          symbol: 'circle',
          edgeShape: 'curve',
          initialTreeDepth: -1,
          orient: 'radial',
          label: {
            position: 'right',
            fontSize: 11,
            color: '#3d4a5c',
            formatter: (p: { data: TreeItem }) => (p.data as TreeItem).name,
          },
          emphasis: { focus: 'descendant' },
          animationDuration: 320,
        },
      ],
    },
    true,
  )
}

function onMouseOver(params: any) {
  const data = params?.data as TreeItem | undefined
  if (!data) return
  if (data.isTx) hoverTarget = { kind: 'tx', id: String(data.txId), txId: data.txId }
  else if (data.nodeId) hoverTarget = { kind: 'node', id: data.nodeId }
}

function onMouseDown(params: any) {
  const data = params?.data as TreeItem | undefined
  if (!data) return
  if (data.isTx) dragFrom = { kind: 'tx', id: String(data.txId), txId: data.txId }
  else if (data.nodeId) dragFrom = { kind: 'node', id: data.nodeId }
}

function onZrMouseUp() {
  const from = dragFrom
  const to = hoverTarget
  dragFrom = null
  if (!from || !to) return
  if (from.kind === 'tx' && to.kind === 'node' && from.id !== to.id) {
    emit('reassign', { txId: Number(from.id), toNodeId: to.id })
  } else if (from.kind === 'tx' && to.kind === 'tx' && from.id !== to.id) {
    emit('group', { txIds: [Number(from.id), Number(to.txId)] })
  }
}

function onClick(params: any) {
  const data = params?.data as TreeItem | undefined
  if (!data) return
  if (data.isTx) emit('select-tx', Number(data.txId))
  else if (data.nodeId) emit('select-node', data.nodeId)
}

function resize() {
  chart?.resize()
}

onMounted(() => {
  render()
  chart?.on('mouseover', onMouseOver)
  chart?.on('mousedown', onMouseDown)
  chart?.on('click', onClick)
  chart?.getZr().on('mouseup', onZrMouseUp)
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})

watch(() => props.tree, render, { deep: true })

// 选中态 + 抖动：用 emphasis/dispatchAction 表达
watch(
  () => [props.selectedNodeId, props.selectedTxId, props.shakeNodeId],
  () => {
    if (!chart) return
    chart.dispatchAction({ type: 'downplay' })
    if (props.shakeNodeId) {
      chart.dispatchAction({
        type: 'highlight',
        seriesIndex: 0,
        dataIndex: findDataIndex(props.shakeNodeId),
      })
    }
  },
)

/** 找到节点在 ECharts 数据里的下标（浅层展开用） */
function findDataIndex(nodeId: string): number {
  const tree = props.tree
  if (!tree) return 0
  return tree.nodes.findIndex(n => n.node_id === nodeId)
}
</script>

<template>
  <div class="star-canvas">
    <div ref="el" class="canvas" />
    <p v-if="!tree" class="empty">还没有账本。先在右上角建一本。</p>
  </div>
</template>

<style scoped>
.star-canvas {
  position: relative;
  min-height: 420px;
  height: 100%;
}
.canvas {
  width: 100%;
  height: 100%;
  min-height: 420px;
}
.empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--color-muted);
  font-size: 13px;
}
</style>
