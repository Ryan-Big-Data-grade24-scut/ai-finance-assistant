<script setup lang="ts">
/**
 * 账本代数 V2 —— 表格视图 / 星云视图 + 主控台。
 *
 * 选择 ⇒ 操作 是这套引擎的核心：移动 / 加入 / 新建 / 分裂 / 解散 / 取消分裂都有一个「已选集合」。
 * - 单击条目 = 选择（Ctrl/⌘ 单击 = 加选 / 取消）；空白处 / 未选中条目上拖动 = 框选；
 * - 选中后按住条目立即就能拖动：拖到 M 上 = 移动，拖到 +2 上 = 加入；非法放置时指针旁给理由；
 * - 中键（滚轮键）按住上下拖 = 滚动表格；星云视图里中键按住拖 = 移动视野；
 * - 写操作之后**保持原来的选中状态**：移动 / 加入 / 新建不动选择；
 *   分裂后加上新生成的 t′；取消分裂后把 t′ 换成原型 t；解散后只剩还活着的那些；
 * - 新建 M / +2 不在这里填名字：点按钮后弹框让你命名（备注可选）；
 * - 单选时右侧「详细信息」面板显示它的命名 / 备注和交易的其它字段。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  applyLedger2Op, getLedger2Tree, redoLedger2, undoLedger2,
  type Ledger2Group, type Ledger2Node, type Ledger2Op, type Ledger2Range,
  type Ledger2Tree, type Ledger2Tx,
} from '@/api/ledger2'
import Ledger2Graph from '@/components/Ledger2Graph.vue'

type Row =
  | { key: string; depth: number; kind: 'node'; node: Ledger2Node; parentId: string | null }
  | { key: string; depth: number; kind: 'group'; group: Ledger2Group; parentId: string }
  | { key: string; depth: number; kind: 'tx'; tx: Ledger2Tx; groupId: string | null; parentId: string }

const data = ref<Ledger2Tree | null>(null)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const view = ref<'table' | 'star'>('table')
const dateFrom = ref('')
const dateTo = ref('')
const expanded = ref<Set<string>>(new Set(['root']))
const selected = ref<Set<string>>(new Set())

const moveTarget = ref('root')
const joinTarget = ref('')

const editing = ref<{ key: string; field: 'name' | 'note' } | null>(null)
const editValue = ref('')
const editInput = ref<HTMLInputElement | null>(null)

const dialog = ref<{ kind: 'node' | 'group'; name: string; note: string } | null>(null)
const dialogName = ref<HTMLInputElement | null>(null)

const consoleName = ref('')
const consoleNote = ref('')

const dragging = ref<Set<string> | null>(null)
const ghost = ref<{ x: number; y: number; text: string; bad: boolean } | null>(null)
const dropTarget = ref<{ key: string; kind: Row['kind'] } | null>(null)
const band = ref<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
const treeEl = ref<HTMLElement | null>(null)
const rowEls = new Map<string, HTMLElement>()

const OP_LABELS: Record<string, string> = {
  init: '初始化', create_node: '新建 M', move: '移动', join: '加入 +2', create_group: '新建 +2',
  dissolve: '解散', split: '分裂', unsplit: '取消分裂', update: '改名/备注',
  create_net_group: '新建 +2', add_group_member: '加入 +2', remove_group_member: '移出 +2',
  dissolve_node: '解散', dissolve_group: '解散', split_tx: '分裂', unsplit_tx: '取消分裂',
  rename_node: '改名', rename_group: '改名', update_node: '改名/备注', update_manual: '改名/备注',
}
const opLabel = (name: string) => OP_LABELS[name] ?? name

const yuan = (cents: number) => (cents / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const pad = (n: number) => String(n).padStart(2, '0')

const range = (): Ledger2Range => (dateFrom.value && dateTo.value
  ? { date_from: dateFrom.value, date_to: dateTo.value } : {})

/**
 * 把仪表盘上的「当前范围 + 已选对象」显式带到账本助手。
 *
 * 只带引用，不带内容：助手会自己用工具重新读一遍账本，页面上下文里只有范围和
 * 稳定引用（节点 id / g:组id / tx:交易id）。这样跨页面看到的数字仍以账本为准，
 * 不会把这一页可能过期的金额当成事实。
 */
const router = useRouter()
function askAssistant() {
  const query: Record<string, string> = {}
  if (dateFrom.value && dateTo.value) {
    query.date_from = dateFrom.value
    query.date_to = dateTo.value
  }
  // 引用列表放在浏览器历史状态。大选区不截断，也不用超长 URL 才能刷新。
  router.push({ path: '/v2/assistant', query, state: { agentcoreSelection: [...selected.value] } })
}

// ── 读 / 写 ───────────────────────────────────────────────────────────────

function accept(next: Ledger2Tree) {
  data.value = next
  const alive = new Set<string>()
  const walk = (n: Ledger2Node) => {
    alive.add(n.id)
    for (const c of n.children) {
      if (c.type === 'node') walk(c)
      else if (c.type === 'group') {
        alive.add(`g:${c.id}`)
        c.members.forEach(m => alive.add(m.ref))
      } else alive.add(c.ref)
    }
  }
  walk(next.tree)
  selected.value = new Set([...selected.value].filter(k => alive.has(k)))
  if (!alive.has(moveTarget.value)) moveTarget.value = 'root'
  if (joinTarget.value && !alive.has(joinTarget.value)) joinTarget.value = ''
  dragging.value = null
  ghost.value = null
  dropTarget.value = null
}

/**
 * 跑一次读或写。**写之后保持原来的选中状态**（不是额外加的函数，而是这里统一收尾）：
 * - 移动 / 加入 / 新建：引擎没删东西，原选择经 accept 的存活过滤后原样留下；
 * - 分裂：原来的 t / M / +2 不动，再补上每个新 t′（`created.split` 是真正裂开的源交易 id）；
 * - 取消分裂：t′ 没了，补上它的原型 t；M / +2 里还活着的子集照常留下；
 * - 解散：被解散的 M / +2 消失，剩下的存活子集经 accept 过滤后自然留下。
 */
async function run(job: () => Promise<Ledger2Tree>, ok?: string) {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    const next = await job()
    const created = next.result?.created
    accept(next)
    // 新建后把新节点展开：被收进新 M / +2 的条目不会「消失」在折叠节点里
    if (created?.node_id || created?.group_id) reveal(created.node_id ?? created.group_id ?? '')
    if (created?.split?.length) {
      selected.value = new Set([...selected.value, ...created.split.map(id => `txm:${id}`)])
    }
    if (created?.unsplit?.length) {
      selected.value = new Set([...selected.value, ...created.unsplit.map(id => `tx:${id}`)])
    }
    if (ok) notice.value = ok
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

const load = () => run(() => getLedger2Tree(range()))
const send = (op: Ledger2Op, ok?: string) => run(() => applyLedger2Op(op, range()), ok)
const undo = () => run(() => undoLedger2(range()), '已撤销')
const redo = () => run(() => redoLedger2(range()), '已重做')

// ── 时间 ─────────────────────────────────────────────────────────────────

const lastMonth = computed(() => data.value?.available.last?.slice(0, 7) ?? '')
const emptyRange = computed(() => !!data.value && !!data.value.scope.date_from
  && data.value.scope.tx_in_scope === 0 && (data.value.scope.tx_total > 0 || data.value.uninit > 0))

function setMonth(ym: string) {
  const [y, m] = ym.split('-').map(Number)
  dateFrom.value = `${ym}-01`
  dateTo.value = `${ym}-${pad(new Date(y, m, 0).getDate())}`
  load()
}
function thisMonth() {
  const d = new Date()
  setMonth(`${d.getFullYear()}-${pad(d.getMonth() + 1)}`)
}
function showAll() {
  dateFrom.value = ''
  dateTo.value = ''
  load()
}

// ── 树 → 行 ───────────────────────────────────────────────────────────────

function rowName(r: Row): string {
  if (r.kind === 'node') return r.node.name
  if (r.kind === 'group') return r.group.name || '（未命名 +2）'
  return r.tx.title || r.tx.counterparty || r.tx.item || `#${r.tx.tx_id}`
}
function rowNote(r: Row): string {
  return r.kind === 'tx' ? (r.tx.memo || '') : r.kind === 'group' ? r.group.note : r.node.note
}

/** 全树索引：折叠不影响它（求和 / 单选面板 / 拖放校验都用这个）。 */
const rowIndex = computed<Map<string, Row>>(() => {
  const m = new Map<string, Row>()
  if (!data.value) return m
  const walk = (n: Ledger2Node, depth: number, parentId: string | null) => {
    m.set(n.id, { key: n.id, depth, kind: 'node', node: n, parentId })
    for (const c of n.children) {
      if (c.type === 'node') walk(c, depth + 1, n.id)
      else if (c.type === 'group') {
        m.set(`g:${c.id}`, { key: `g:${c.id}`, depth: depth + 1, kind: 'group', group: c, parentId: n.id })
        for (const t of c.members) m.set(t.ref, { key: t.ref, depth: depth + 2, kind: 'tx', tx: t, groupId: c.id, parentId: c.node_id })
      } else {
        m.set(c.ref, { key: c.ref, depth: depth + 1, kind: 'tx', tx: c, groupId: null, parentId: n.id })
      }
    }
  }
  walk(data.value.tree, 0, null)
  return m
})

const parentOf = computed<Map<string, string>>(() => {
  const m = new Map<string, string>()
  for (const [k, r] of rowIndex.value) if (r.kind === 'node' && r.parentId) m.set(k, r.parentId)
  return m
})
function isDescendant(node: string, ancestor: string): boolean {
  let cur: string | undefined = node
  while (cur) {
    if (cur === ancestor) return true
    cur = parentOf.value.get(cur)
  }
  return false
}
const nodeName = (id: string) => {
  const r = rowIndex.value.get(id)
  return r && r.kind === 'node' ? r.node.name : id
}

const rows = computed<Row[]>(() => {
  const out: Row[] = []
  if (!data.value) return out
  const walk = (n: Ledger2Node, depth: number, parentId: string | null) => {
    out.push({ key: n.id, depth, kind: 'node', node: n, parentId })
    if (!expanded.value.has(n.id)) return
    for (const c of n.children) {
      if (c.type === 'node') walk(c, depth + 1, n.id)
      else if (c.type === 'group') {
        out.push({ key: `g:${c.id}`, depth: depth + 1, kind: 'group', group: c, parentId: n.id })
        if (expanded.value.has(c.id)) {
          for (const t of c.members) {
            out.push({ key: t.ref, depth: depth + 2, kind: 'tx', tx: t, groupId: c.id, parentId: c.node_id })
          }
        }
      } else out.push({ key: c.ref, depth: depth + 1, kind: 'tx', tx: c, groupId: null, parentId: n.id })
    }
  }
  walk(data.value.tree, 0, null)
  return out
})

const nodeOptions = computed(() => {
  const out: { id: string; label: string }[] = []
  for (const r of rowIndex.value.values()) {
    if (r.kind === 'node') out.push({ id: r.node.id, label: `${'　'.repeat(r.depth)}${r.node.name}` })
  }
  return out
})
const groupOptions = computed(() => {
  const out: { id: string; label: string }[] = []
  for (const r of rowIndex.value.values()) {
    if (r.kind === 'group') out.push({ id: r.group.id, label: `${'　'.repeat(r.depth)}${r.group.name || '（未命名 +2）'}` })
  }
  return out
})

// ── 选择 ─────────────────────────────────────────────────────────────────

const selList = computed(() => [...selected.value])
const selRows = computed(() => selList.value.map(k => rowIndex.value.get(k)).filter((r): r is Row => !!r))
const selAllTx = computed(() => selRows.value.length > 0 && selRows.value.every(r => r.kind === 'tx'))
const single = computed(() => (selRows.value.length === 1 ? selRows.value[0] : null))
const singleIsMirror = computed(() => (single.value?.kind === 'tx' ? single.value.tx.role === 'mirror' : false))
const singleIsRoot = computed(() => (single.value?.kind === 'node' ? single.value.node.kind === 'root' : false))
const singleLabel = computed(() => {
  const r = single.value
  if (!r) return ''
  if (r.kind === 'group') return '+2'
  if (r.kind === 'tx') return r.tx.role === 'mirror' ? '交易 t′' : '交易 t'
  return r.node.kind === 'root' ? '总账根' : r.node.kind === 'category' ? '分类节点' : 'M（分组）'
})
const singleWhere = computed(() => {
  const r = single.value
  if (!r) return ''
  if (r.kind === 'tx') {
    const base = nodeName(r.parentId)
    const g = r.groupId ? rowIndex.value.get(`g:${r.groupId}`) : null
    return g?.kind === 'group' ? `${base} / ${g.group.name || '（未命名 +2）'}` : base
  }
  if (r.kind === 'group') return nodeName(r.group.node_id)
  return r.parentId ? nodeName(r.parentId) : '（顶层）'
})

const selSum = computed(() => {
  let e = 0, i = 0
  for (const r of selRows.value) {
    if (r.kind === 'tx') { e += r.tx.e; i += r.tx.i }
    else if (r.kind === 'node') { e += r.node.e; i += r.node.i }
    else { e += r.group.e; i += r.group.i }
  }
  return { e, i }
})

watch(single, () => {
  consoleName.value = single.value ? rowName(single.value) : ''
  consoleNote.value = single.value ? rowNote(single.value) : ''
}, { immediate: true })

function toggleSel(key: string) {
  const s = new Set(selected.value)
  s.has(key) ? s.delete(key) : s.add(key)
  selected.value = s
}
function clearSelection() {
  selected.value = new Set()
}

function expandAll(open: boolean) {
  if (!data.value) return
  const s = new Set<string>(['root'])
  if (open) {
    for (const r of rowIndex.value.values()) {
      if (r.kind === 'node') s.add(r.node.id)
      else if (r.kind === 'group') s.add(r.group.id)
    }
  }
  expanded.value = s
}
function toggleOpen(id: string) {
  const s = new Set(expanded.value)
  s.has(id) ? s.delete(id) : s.add(id)
  expanded.value = s
}
/** 展开一个节点 / +2（`g:<id>` 也接受），保证刚放进去的条目看得见。 */
function reveal(keyOrId: string) {
  if (!keyOrId) return
  const id = keyOrId.startsWith('g:') ? keyOrId.slice(2) : keyOrId
  const s = new Set(expanded.value)
  s.add(id)
  expanded.value = s
}

// ── 交互：单击选择 / 按住拖动 / 拖框选择 ─────────────────────────────────

const DRAG_MIN = 4
let press: { key: string; x: number; y: number; moved: boolean; wantDrag: boolean; additive: boolean } | null = null
let bandAdditive = false

function setRowEl(key: string, el: any) {
  if (el) rowEls.set(key, el as HTMLElement)
  else rowEls.delete(key)
}
function setEditInput(el: any) {
  editInput.value = (el as HTMLInputElement) ?? null
}

function clearPress() {
  window.removeEventListener('pointermove', pressMove)
  window.removeEventListener('pointerup', pressUp)
  window.removeEventListener('pointercancel', pressUp)
  press = null
}

function clickSelect(key: string, additive: boolean) {
  if (additive) {
    toggleSel(key)
    return
  }
  // 已选中的一项（或多选里的某一项）单击不塌缩，方便「选好一组再拖动」。
  if (selected.value.has(key)) return
  selected.value = new Set([key])
}

/**
 * 条目按下：选中的条目 = 立刻可以拖（不等长按）；未选中的条目 = 立刻起框选。
 * 松开时如果没移动，就按「单击」处理。
 */
function itemDown(e: PointerEvent, key: string) {
  if (e.button !== 0 || editing.value) return
  if ((e.target as Element).closest('button, input, select, a, textarea')) return
  clearPress()
  press = {
    key, x: e.clientX, y: e.clientY, moved: false,
    wantDrag: selected.value.has(key),
    additive: e.ctrlKey || e.metaKey || e.shiftKey,
  }
  window.addEventListener('pointermove', pressMove)
  window.addEventListener('pointerup', pressUp)
  window.addEventListener('pointercancel', pressUp)
}

function pressMove(e: PointerEvent) {
  const p = press
  if (!p) return
  if (!p.moved && Math.hypot(e.clientX - p.x, e.clientY - p.y) > DRAG_MIN) p.moved = true
  if (!p.moved) return
  if (p.wantDrag) {
    if (!dragging.value) dragging.value = new Set(selected.value)
    updateGhost(e.clientX, e.clientY)
  } else {
    band.value = { x0: p.x, y0: p.y, x1: e.clientX, y1: e.clientY }
  }
}

function pressUp(e: PointerEvent) {
  const p = press
  const target = dropTarget.value
  const b = band.value
  clearPress()
  if (p) {
    if (p.wantDrag) {
      if (!p.moved) clickSelect(p.key, p.additive)
      else if (target) performDrop(target.key, target.kind)
    } else if (!p.moved) {
      clickSelect(p.key, p.additive)
    } else if (b) {
      applyBand(b, p.additive)
    }
  }
  band.value = null
  dragging.value = null
  dropTarget.value = null
  ghost.value = null
}

/** 跟随指针的提示：悬停的放置目标 + 合法 / 非法原因。 */
function updateGhost(x: number, y: number) {
  let text = `拖动 ${dragging.value?.size ?? 0} 项：放到 M 上移动，放到 +2 上加入`
  let bad = false
  let target: { key: string; kind: Row['kind'] } | null = null
  const el = document.elementFromPoint(x, y)?.closest('[data-drop-key]') as HTMLElement | null
  if (el?.dataset.dropKey) {
    const key = el.dataset.dropKey
    const kind = (el.dataset.dropKind || 'node') as Row['kind']
    const reason = dropReason(key, kind)
    if (reason) {
      text = reason
      bad = true
    } else {
      target = { key, kind }
      text = kind === 'group' ? '放开：加入这个 +2' : '放开：移到这里'
    }
  }
  dropTarget.value = target
  ghost.value = { x: x + 16, y: y + 16, text, bad }
}

function draggedKeys(): string[] {
  return dragging.value ? [...dragging.value] : []
}

/** 返回非法原因；null = 合法。 */
function dropReason(targetKey: string, kind: Row['kind']): string | null {
  const keys = draggedKeys()
  if (!keys.length) return '没有要拖动的项'
  if (kind === 'tx') return '交易不能作为放置目标：请拖到 M（移动）或 +2（加入）上'
  const rowsIn = keys.map(k => rowIndex.value.get(k)).filter((r): r is Row => !!r)
  if (kind === 'node') {
    const nodeRow = rowIndex.value.get(targetKey)
    if (!nodeRow || nodeRow.kind !== 'node') return '这个位置不能放'
    for (const r of rowsIn) {
      if (r.kind !== 'node') continue
      if (r.node.id === nodeRow.node.id) return '不能移到自己里面'
      if (isDescendant(nodeRow.node.id, r.node.id)) return '不能把 M 移到它自己的子树里'
    }
    return null
  }
  const gRow = rowIndex.value.get(targetKey)
  if (!gRow || gRow.kind !== 'group') return '这个位置不能放'
  const g = gRow.group
  for (const r of rowsIn) {
    if (r.kind !== 'tx') return '加入 +2 只能选交易（不能有 M / +2）'
    if (g.members.some(m => m.ref === r.tx.ref)) return '这笔交易已经在组里了'
    if (r.parentId !== g.node_id) {
      return `只能加入同父的交易：「${nodeName(r.parentId)}」和组所在的「${nodeName(g.node_id)}」不同`
    }
  }
  return null
}

function performDrop(key: string, kind: Row['kind']) {
  const keys = draggedKeys()
  if (!keys.length || dropReason(key, kind)) return
  if (kind === 'group') {
    const row = rowIndex.value.get(key)
    if (!row || row.kind !== 'group') return
    void send({ type: 'join', items: keys, group_id: row.group.id }, '已加入 +2')
      .then(() => { if (!error.value) reveal(key) })
  } else if (kind === 'node') {
    void send({ type: 'move', items: keys, target: key }, '已移动')
      .then(() => { if (!error.value) reveal(key) })
  }
}

/** 把一个屏幕矩形内的行并进选择（框选的共同实现）。 */
function applyBand(b: { x0: number; y0: number; x1: number; y1: number }, additive: boolean) {
  const left = Math.min(b.x0, b.x1), right = Math.max(b.x0, b.x1)
  const top = Math.min(b.y0, b.y1), bottom = Math.max(b.y0, b.y1)
  const hits = new Set<string>()
  for (const [key, el] of rowEls) {
    if (key === 'root') continue
    const r = el.getBoundingClientRect()
    if (r.bottom > top && r.top < bottom && r.right > left && r.left < right) hits.add(key)
  }
  selected.value = additive ? new Set([...selected.value, ...hits]) : hits
}

// 表格空白处：按下拖框选择；不动松开 = 清空选择。
function bandStart(e: PointerEvent) {
  if (e.button !== 0) return
  if ((e.target as Element).closest('.row')) return
  bandAdditive = e.ctrlKey || e.metaKey
  band.value = { x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY }
  window.addEventListener('pointermove', bandMove)
  window.addEventListener('pointerup', bandEnd)
  window.addEventListener('pointercancel', bandEnd)
}
function bandMove(e: PointerEvent) {
  if (!band.value) return
  band.value = { ...band.value, x1: e.clientX, y1: e.clientY }
}
function bandEnd() {
  window.removeEventListener('pointermove', bandMove)
  window.removeEventListener('pointerup', bandEnd)
  window.removeEventListener('pointercancel', bandEnd)
  const b = band.value
  band.value = null
  if (!b) return
  const w = Math.abs(b.x1 - b.x0), h = Math.abs(b.y1 - b.y0)
  if (w < 4 && h < 4) {
    if (!bandAdditive) selected.value = new Set()
    return
  }
  applyBand(b, bandAdditive)
}

// 中键（滚轮键）按住上下拖 = 滚动表格，和滚轮上下滚一样。
let midDrag: { y: number; top: number; el: HTMLElement } | null = null
function onBodyDown(e: PointerEvent) {
  if (e.button === 1) {
    e.preventDefault()
    const el = e.currentTarget as HTMLElement
    midDrag = { y: e.clientY, top: el.scrollTop, el }
    window.addEventListener('pointermove', midMove)
    window.addEventListener('pointerup', midUp)
    window.addEventListener('pointercancel', midUp)
    return
  }
  bandStart(e)
}
function midMove(e: PointerEvent) {
  if (midDrag) midDrag.el.scrollTop = midDrag.top - (e.clientY - midDrag.y)
}
function midUp() {
  window.removeEventListener('pointermove', midMove)
  window.removeEventListener('pointerup', midUp)
  window.removeEventListener('pointercancel', midUp)
  midDrag = null
}
onBeforeUnmount(() => {
  clearPress()
  midUp()
  window.removeEventListener('pointermove', bandMove)
  window.removeEventListener('pointerup', bandEnd)
  window.removeEventListener('pointercancel', bandEnd)
})

/** 星云视图展开 / 折叠（和表格的 ▸ / ▾ 同一份 expanded）。 */
function onGraphToggle(key: string) {
  toggleOpen(key.startsWith('g:') ? key.slice(2) : key)
}

/** 星云视图拖框选择：keys 为空 = 点空白清空选择。 */
function onGraphBand(keys: string[], additive: boolean) {
  if (!keys.length) {
    if (!additive) selected.value = new Set()
    return
  }
  selected.value = additive ? new Set([...selected.value, ...keys]) : new Set(keys)
}

const bandStyle = computed(() => {
  const b = band.value
  if (!b) return {}
  return {
    left: `${Math.min(b.x0, b.x1)}px`, top: `${Math.min(b.y0, b.y1)}px`,
    width: `${Math.abs(b.x1 - b.x0)}px`, height: `${Math.abs(b.y1 - b.y0)}px`,
  }
})

// ── 新建（弹框命名）──────────────────────────────────────────────────────

function openCreate(kind: 'node' | 'group') {
  if (!selList.value.length || loading.value) return
  dialog.value = { kind, name: '', note: '' }
  nextTick(() => dialogName.value?.focus())
}
function confirmCreate() {
  const d = dialog.value
  if (!d) return
  const name = d.name.trim()
  if (!name) return
  const note = d.note.trim()
  const items = selList.value
  dialog.value = null
  if (d.kind === 'node') send({ type: 'create_node', name, note: note || undefined, items }, `已新建「${name}」`)
  else send({ type: 'create_group', name, note: note || undefined, items }, `已新建 +2「${name}」`)
}
function cancelCreate() {
  dialog.value = null
}

// ── 主控台动作 ───────────────────────────────────────────────────────────

const doMove = async () => {
  if (!selList.value.length || !moveTarget.value) return
  await send({ type: 'move', items: selList.value, target: moveTarget.value }, '已移动')
  if (!error.value) reveal(moveTarget.value)      // 移进去的东西保持可见
}
const doJoin = async () => {
  if (!selList.value.length || !joinTarget.value) return
  await send({ type: 'join', items: selList.value, group_id: joinTarget.value }, '已加入 +2')
  if (!error.value) reveal(joinTarget.value)
}
const doSplit = () => selList.value.length && send({ type: 'split', items: selList.value }, '已分裂')
const doDissolve = () => selList.value.length && send({ type: 'dissolve', items: selList.value }, '已解散')
const doUnsplit = () => selList.value.length && send({ type: 'unsplit', items: selList.value }, '已取消分裂')
const doInit = () => send({ type: 'init', style: 'platform' }, '已按平台分类初始化')

// ── 改名 / 备注 ──────────────────────────────────────────────────────────

function commitUpdate(item: string, field: 'name' | 'note', value: string) {
  const row = rowIndex.value.get(item)
  if (!row) return
  const current = field === 'name' ? rowName(row) : rowNote(row)
  if (value === current) return
  if (row.kind === 'tx' && row.tx.role === 'mirror') {
    error.value = 't′ 没有人工字段，只能改它所属的 t。'
    return
  }
  send({ type: 'update', item, [field]: value }, field === 'name' ? '已命名' : '备注已更新')
}

function beginEdit(key: string, field: 'name' | 'note') {
  const row = rowIndex.value.get(key)
  if (!row) return
  if (row.kind === 'tx' && row.tx.role === 'mirror') return
  if (field === 'name' && row.kind === 'node' && row.node.kind === 'root') return
  editing.value = { key, field }
  editValue.value = field === 'name' ? rowName(row) : rowNote(row)
  nextTick(() => {
    editInput.value?.focus()
    editInput.value?.select()
  })
}
function commitEdit(save: boolean) {
  const ed = editing.value
  if (!ed) return
  editing.value = null
  if (!save) return
  commitUpdate(ed.key, ed.field, editValue.value)
}

function commitConsole(field: 'name' | 'note') {
  if (!single.value) return
  const value = field === 'name' ? consoleName.value : consoleNote.value
  commitUpdate(single.value.key, field, value)
}

// ── 生命周期 ─────────────────────────────────────────────────────────────

onMounted(() => {
  const d = new Date()
  setMonth(`${d.getFullYear()}-${pad(d.getMonth() + 1)}`)
})
</script>

<template>
  <section class="l2">
    <div class="l2-layout">
      <!-- 左：视图切换 -->
      <nav class="l2-tabs" data-testid="view-select" aria-label="视图">
        <button class="l2-tab" :class="{ active: view === 'table' }" data-testid="view-table"
                @click="view = 'table'">
          <span class="ico">▤</span><span>表格</span>
        </button>
        <button class="l2-tab" :class="{ active: view === 'star' }" data-testid="view-star"
                @click="view = 'star'">
          <span class="ico">✦</span><span>星图</span>
        </button>
      </nav>

      <!-- 中：账本视图（占满剩余高度，滚动只发生在视图内部） -->
      <div class="l2-main">
        <p v-if="emptyRange" class="banner banner--warn" data-testid="empty-month">
          这个范围里没有数据。<template v-if="lastMonth">最近有数据的月份是 {{ lastMonth }}。</template>
          <button v-if="lastMonth" class="link" @click="setMonth(lastMonth)">跳过去</button>
        </p>
        <p v-if="error" class="banner banner--err" data-testid="error">{{ error }}</p>
        <p v-else-if="notice" class="banner banner--ok">{{ notice }}</p>
        <ul v-if="data?.status.warnings.length" class="banner banner--warn" data-testid="warnings">
          <li v-for="w in data.status.warnings" :key="w">{{ w }}</li>
        </ul>

        <div v-if="view === 'table'" class="l2-tree" data-testid="tree">
          <!-- 仅展示现有状态，不改变账本数据、选择或拖拽行为 -->
          <div class="ledger-toolbar">
            <div class="ledger-toolbar__heading">
              <span class="ledger-toolbar__icon" aria-hidden="true">▤</span>
              <div class="ledger-toolbar__titles">
                <strong>账本明细</strong>
                <small>分类清晰，收支一目了然</small>
              </div>
            </div>
            <div class="ledger-toolbar__stats">
              <span v-if="data">{{ data.scope.tx_in_scope }} 笔交易</span>
              <span v-if="selList.length" class="ledger-toolbar__selected">已选 {{ selList.length }} 项</span>
            </div>
          </div>
          <div class="thead">
            <span class="c-grip" /><span class="c-check" /><span class="c-name">命名</span>
            <span class="c-note">备注</span><span class="c-meta">分类 / 日期</span>
            <span class="c-num">支出</span><span class="c-num">收入</span>
          </div>
          <div ref="treeEl" class="tbody" @pointerdown="onBodyDown" @mousedown.middle.prevent @auxclick.prevent>
            <p v-if="loading && !data" class="muted pad">加载中…</p>
            <div v-for="r in rows" :key="r.key" :ref="el => setRowEl(r.key, el)"
                 class="row"
                 :class="[
                   `row--${r.kind}`,
                   {
                     'row--root': r.kind === 'node' && r.node.kind === 'root',
                     'row--category': r.kind === 'node' && r.node.kind === 'category',
                     'row--sel': selected.has(r.key),
                     'row--drag': dragging?.has(r.key),
                     'row--drop': dropTarget?.key === r.key,
                   },
                 ]"
                 :data-key="r.key" :data-drop-key="r.key" :data-drop-kind="r.kind"
                 @pointerdown="itemDown($event, r.key)">
              <span class="c-grip">
                <span class="grip" title="选中后按住即可拖动">⠿</span>
              </span>
              <span class="c-check">
                <input v-if="!(r.kind === 'node' && r.node.kind === 'root')" type="checkbox"
                       :checked="selected.has(r.key)" @change="toggleSel(r.key)">
              </span>
              <span class="c-name" :style="{ paddingLeft: `${r.depth * 18}px` }">
                <button v-if="r.kind === 'node'" class="twisty" @click="toggleOpen(r.node.id)">
                  {{ r.node.children.length ? (expanded.has(r.node.id) ? '▾' : '▸') : '·' }}
                </button>
                <button v-else-if="r.kind === 'group'" class="twisty" @click="toggleOpen(r.group.id)">
                  {{ expanded.has(r.group.id) ? '▾' : '▸' }}
                </button>
                <span v-else class="twisty" />
                <span v-if="r.kind === 'node' && r.node.kind === 'category'" class="tag tag--cat">分类</span>
                <span v-if="r.kind === 'group'" class="tag tag--net">+2</span>
                <span v-if="r.kind === 'tx' && r.tx.role === 'mirror'" class="tag tag--m">t′</span>
                <input v-if="editing && editing.key === r.key && editing.field === 'name'" :ref="setEditInput"
                       v-model="editValue" class="inline-edit" @keyup.enter="commitEdit(true)"
                       @keyup.esc="commitEdit(false)" @blur="commitEdit(true)">
                <span v-else class="name" :class="[`name--${r.kind}`, { 'name--root': r.kind === 'node' && r.node.kind === 'root' }]"
                      :title="'双击改命名'" @dblclick="beginEdit(r.key, 'name')">{{ rowName(r) }}</span>
              </span>
              <span class="c-note">
                <input v-if="editing && editing.key === r.key && editing.field === 'note'" :ref="setEditInput"
                       v-model="editValue" class="inline-edit" @keyup.enter="commitEdit(true)"
                       @keyup.esc="commitEdit(false)" @blur="commitEdit(true)">
                <span v-else class="note" :title="'双击改备注'" @dblclick="beginEdit(r.key, 'note')">
                  {{ rowNote(r) || '—' }}
                </span>
              </span>
              <span class="c-meta muted">
                <template v-if="r.kind === 'tx'">{{ r.tx.date }} · {{ r.tx.category || '其他' }}</template>
                <template v-else>{{ r.kind === 'node' ? r.node.count : r.group.count }} 笔{{ r.kind === 'group' ? ' · ν' : '' }}</template>
              </span>
              <span class="c-num e">{{ r.kind === 'node' ? (r.node.e ? '¥' + yuan(r.node.e) : '—')
                : r.kind === 'group' ? (r.group.e ? '¥' + yuan(r.group.e) : '—') : (r.tx.e ? '¥' + yuan(r.tx.e) : '—') }}</span>
              <span class="c-num i">{{ r.kind === 'node' ? (r.node.i ? '¥' + yuan(r.node.i) : '—')
                : r.kind === 'group' ? (r.group.i ? '¥' + yuan(r.group.i) : '—') : (r.tx.i ? '¥' + yuan(r.tx.i) : '—') }}</span>
            </div>
            <p v-if="data && !rows.length" class="muted pad">这个范围里没有可显示的行。</p>
          </div>
        </div>

        <Ledger2Graph v-else :tree="data?.tree ?? null" :expanded="expanded" :selected="selected"
                      :dragging="dragging" :drop-target="dropTarget?.key ?? ''"
                      data-testid="graph" @item-down="itemDown" @band-select="onGraphBand"
                      @toggle="onGraphToggle" />
      </div>

      <!-- 右：时间 / 操控 / 详情 / 历史 -->
      <aside class="l2-side">
        <section class="panel panel--date">
          <h3 class="panel-title"><i class="dot dot--date" />时间区间</h3>
          <div class="field-row">
            <label class="field">从<input v-model="dateFrom" type="date" data-testid="date-from"></label>
            <label class="field">到<input v-model="dateTo" type="date" data-testid="date-to"></label>
          </div>
          <div class="mini-grid">
            <button :disabled="loading || !dateFrom || !dateTo" data-testid="apply-range" @click="load">按范围读</button>
            <button :disabled="loading" @click="thisMonth">本月</button>
            <button :disabled="loading || !lastMonth" @click="setMonth(lastMonth)">最近有数据</button>
            <button :disabled="loading" @click="showAll">全部</button>
          </div>
          <div class="mini-grid">
            <button @click="expandAll(true)">全部展开</button>
            <button @click="expandAll(false)">全部折叠</button>
          </div>
          <p v-if="data" class="muted scope-line" data-testid="scope-info">
            {{ data.scope.date_from ? `${data.scope.date_from} ~ ${data.scope.date_to}` : '全部时间' }}
            · 范围内 {{ data.scope.tx_in_scope }} / 共 {{ data.scope.tx_total }} 笔
          </p>
          <button v-if="data && data.uninit > 0" class="btn--init" :disabled="loading" data-testid="init"
                  title="按平台分类初始化所有未初始化的交易（可撤销）" @click="doInit">
            初始化 {{ data.uninit }} 笔
          </button>
          <button class="btn--ask" data-testid="ask-assistant"
                  :title="`把当前范围和 ${selList.length} 项选择带到账本助手`" @click="askAssistant">
            ✎ 带着当前范围问助手
          </button>
        </section>

        <section class="panel panel--ops" data-testid="console">
          <h3 class="panel-title"><i class="dot dot--ops" />操控面板</h3>
          <div class="sel-line">
            <b>已选 {{ selList.length }} 项</b>
            <span v-if="selList.length" class="muted">支 ¥{{ yuan(selSum.e) }} · 收 ¥{{ yuan(selSum.i) }}</span>
            <button v-if="selList.length" class="link" @click="clearSelection">清除</button>
          </div>
          <div class="op-grid">
            <button :disabled="loading || !selList.length" data-testid="create-node" @click="openCreate('node')">新建 M</button>
            <button :disabled="loading || !selAllTx || selRows.length < 2" data-testid="create-group"
                    @click="openCreate('group')">新建 +2</button>
            <button :disabled="loading || !selList.length" data-testid="split" @click="doSplit">分裂</button>
            <button :disabled="loading || !selList.length" data-testid="dissolve" @click="doDissolve">解散</button>
            <button :disabled="loading || !selList.length" data-testid="unsplit" @click="doUnsplit">取消分裂</button>
          </div>
          <div class="op-line">
            <select v-model="moveTarget" data-testid="move-target" title="移动到的目标 M">
              <option v-for="o in nodeOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
            </select>
            <button :disabled="loading || !selList.length" data-testid="move" @click="doMove">移动到</button>
          </div>
          <div class="op-line">
            <select v-model="joinTarget" data-testid="join-target" title="加入到的 +2">
              <option value="">选择 +2…</option>
              <option v-for="g in groupOptions" :key="g.id" :value="g.id">{{ g.label }}</option>
            </select>
            <button :disabled="loading || !joinTarget || !selAllTx" data-testid="join" @click="doJoin">加入到</button>
          </div>
          <div class="op-line">
            <button :disabled="loading || !data?.status.can_undo" data-testid="undo" @click="undo">↶ 撤销</button>
            <button :disabled="loading || !data?.status.can_redo" data-testid="redo" @click="redo">↷ 重做</button>
          </div>
          <p class="muted hint-line">
            单击＝选择（Ctrl/⌘ 单击＝加选）；选中后按住即可拖动，拖到 M 上＝移动、拖到 +2 上＝加入。
            未选中的条目上直接拖＝框选；中键按住上下拖＝滚动。
          </p>
        </section>

        <section class="panel panel--detail" data-testid="detail">
          <h3 class="panel-title"><i class="dot dot--detail" />详细信息</h3>
          <template v-if="single">
            <div class="detail-head">
              <span class="pill">{{ singleLabel }}</span>
              <span v-if="singleIsMirror" class="muted">t′ 没有人工字段</span>
            </div>
            <label class="d-row">
              <span>命名</span>
              <input v-model="consoleName" class="d-input" :disabled="singleIsRoot || singleIsMirror"
                     placeholder="命名" data-testid="console-name"
                     @keyup.enter="commitConsole('name')" @blur="commitConsole('name')">
            </label>
            <label class="d-row">
              <span>备注</span>
              <input v-model="consoleNote" class="d-input" :disabled="singleIsMirror"
                     placeholder="备注" data-testid="console-note"
                     @keyup.enter="commitConsole('note')" @blur="commitConsole('note')">
            </label>
            <dl class="d-list">
              <template v-if="single.kind === 'tx'">
                <dt>交易对方</dt><dd>{{ single.tx.counterparty || '—' }}</dd>
                <dt>商品说明</dt><dd>{{ single.tx.item || '—' }}</dd>
                <dt>时间</dt><dd>{{ single.tx.tx_time || single.tx.date }}</dd>
                <dt>金额</dt><dd>支 ¥{{ yuan(single.tx.e) }} · 收 ¥{{ yuan(single.tx.i) }}</dd>
                <dt>平台分类</dt><dd>{{ single.tx.category_path.length ? single.tx.category_path.join(' / ') : (single.tx.category || '其他') }}</dd>
                <dt>来源</dt><dd>{{ single.tx.source || '—' }}</dd>
                <dt>原始备注</dt><dd>{{ single.tx.note || '—' }}</dd>
                <dt>身份</dt><dd>{{ single.tx.role === 'mirror' ? 't′（分裂镜像）' : 't（原始交易）' }}</dd>
              </template>
              <template v-else-if="single.kind === 'group'">
                <dt>金额（ν）</dt><dd>支 ¥{{ yuan(single.group.e) }} · 收 ¥{{ yuan(single.group.i) }}</dd>
                <dt>成员</dt><dd>{{ single.group.count }} 笔</dd>
              </template>
              <template v-else>
                <dt>金额</dt><dd>支 ¥{{ yuan(single.node.e) }} · 收 ¥{{ yuan(single.node.i) }}</dd>
                <dt>笔数</dt><dd>{{ single.node.count }} 笔</dd>
                <dt>类型</dt><dd>{{ single.node.kind === 'root' ? '总账根' : single.node.kind === 'category' ? '平台分类' : 'M（分组）' }}</dd>
                <dt>子项</dt><dd>{{ single.node.children.length }} 项</dd>
              </template>
              <dt>所在</dt><dd>{{ singleWhere }}</dd>
            </dl>
          </template>
          <p v-else class="muted">
            {{ selList.length ? '已选多项，汇总见操控面板。' : '单选一项时显示命名、备注和明细。' }}
          </p>
        </section>

        <section class="panel panel--hist" data-testid="history">
          <h3 class="panel-title"><i class="dot dot--hist" />操作历史</h3>
          <p v-if="!data?.status.history.length" class="muted">还没有操作</p>
          <ol v-else>
            <li v-for="h in [...data.status.history].reverse()" :key="h.seq" :class="{ undone: h.state === 'undone' }">
              <b>#{{ h.seq }}</b> {{ opLabel(h.name) }}
              <small class="muted">{{ h.prims }} 个原语<template v-if="h.state === 'undone'"> · 已撤销</template></small>
            </li>
          </ol>
        </section>
      </aside>
    </div>

    <!-- 框选矩形 -->
    <div v-if="band" class="band" :style="bandStyle" />

    <!-- 拖动提示（跟着鼠标；非法放置时变红） -->
    <div v-if="ghost" class="drag-hint" :class="{ 'drag-hint--bad': ghost.bad }"
         :style="{ left: `${ghost.x}px`, top: `${ghost.y}px` }">{{ ghost.text }}</div>

    <!-- 新建弹框 -->
    <div v-if="dialog" class="modal-mask" @mousedown.self="cancelCreate">
      <div class="modal" data-testid="create-dialog">
        <h3>{{ dialog.kind === 'node' ? '新建 M（分组）' : '新建 +2（净额组）' }}</h3>
        <p class="muted">已选 {{ selList.length }} 项。名字必填，备注可选。</p>
        <label class="field">命名 <input ref="dialogName" v-model="dialog.name" class="modal-input"
                                      @keyup.enter="confirmCreate" @keyup.esc="cancelCreate"></label>
        <label class="field">备注 <input v-model="dialog.note" class="modal-input"
                                      @keyup.enter="confirmCreate" @keyup.esc="cancelCreate"></label>
        <div class="modal-actions">
          <button @click="cancelCreate">取消</button>
          <button class="btn--primary" :disabled="!dialog.name.trim()" data-testid="create-confirm"
                  @click="confirmCreate">确定</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 整页高度 = 视口 − 顶栏(70) − 主区上下内边距(18+34)；页面本身不滚动，滚动都在视图内部。 */
.l2 {
  display: flex; flex-direction: column;
  height: calc(100dvh - 122px); min-height: 420px; overflow: hidden;
}
.l2-layout {
  flex: 1; min-height: 0;
  display: grid; grid-template-columns: 64px minmax(0, 1fr) 320px; gap: 12px;
}

/* 左侧视图切换 */
.l2-tabs { display: flex; flex-direction: column; gap: 8px; min-height: 0; }
.l2-tab {
  display: flex; flex-direction: column; align-items: center; gap: 5px;
  padding: 13px 4px; border: 1px solid var(--color-border); border-radius: 12px;
  background: var(--color-card); color: var(--color-muted); font-size: 13px; cursor: pointer;
  transition: background .15s, color .15s, border-color .15s;
}
.l2-tab .ico { font-size: 18px; line-height: 1; }
.l2-tab:hover:not(.active) { background: #eef3f9; color: var(--color-text); }
.l2-tab.active {
  background: #1d5fa8; border-color: #1d5fa8; color: #fff;
  box-shadow: 0 6px 16px rgba(29, 95, 168, .22);
}

/* 中间视图 */
.l2-main { min-width: 0; min-height: 0; display: flex; flex-direction: column; gap: 8px; }
.l2-main > .banner { flex: none; }

/* 右侧栏 */
.l2-side {
  min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 8px;
  padding-right: 2px;
}
.panel {
  flex: none; background: var(--color-card); border: 1px solid var(--color-border);
  border-radius: 10px; padding: 10px 12px;
}
.panel-title {
  display: flex; align-items: center; gap: 6px; margin: 0 0 8px;
  font-size: 13px; font-weight: 650; color: #344054;
}
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.dot--date { background: #0d9488; }
.dot--ops { background: #2563eb; }
.dot--detail { background: #7c6cf0; }
.dot--hist { background: #94a3b8; }

.field-row { display: flex; flex-direction: column; gap: 4px; }
.field { font-size: 13px; display: inline-flex; align-items: center; gap: 6px; }
.field input[type='date'] { flex: 1; min-width: 0; }
.mini-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-top: 6px; }
.mini-grid button, .op-line button { width: 100%; }
.scope-line { margin: 8px 0 0; line-height: 1.5; }
.btn--init { margin-top: 6px; width: 100%; border-color: #1d5fa8; color: #1d5fa8; }
.btn--ask { margin-top: 6px; width: 100%; }


.sel-line { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13px; }
.op-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin: 8px 0; }
.op-grid button { width: 100%; }
.op-line { display: flex; gap: 6px; align-items: center; margin-top: 6px; }
.op-line select { flex: 1; min-width: 0; }
.hint-line { margin: 8px 0 0; line-height: 1.55; }

.detail-head { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.pill {
  font-size: 11px; padding: 1px 8px; border-radius: 999px;
  background: #eef2ff; color: #4338ca; font-weight: 600;
}
.d-row { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-top: 4px; }
.d-row > span { width: 36px; flex: none; color: var(--color-muted); font-size: 12px; }
.d-input { flex: 1; min-width: 0; }
.d-list {
  display: grid; grid-template-columns: 72px minmax(0, 1fr);
  gap: 3px 8px; margin: 8px 0 0; font-size: 12.5px; line-height: 1.45;
}
.d-list dt { color: var(--color-muted); }
.d-list dd { margin: 0; word-break: break-all; }

.panel--hist ol {
  margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px;
  max-height: 280px; overflow: auto;
}
.panel--hist li { font-size: 13px; }
.panel--hist li.undone { opacity: .5; text-decoration: line-through; }
.panel--hist small { display: block; }

/* 账本代数 V2 的视觉增强：只影响中间表格，不触碰账本计算或操作逻辑。 */
.l2-tree {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
  border: 1px solid #dce5f0;
  border-radius: 14px;
  box-shadow: 0 10px 28px rgba(32, 60, 99, .055);
}
.ledger-toolbar {
  flex: none;
  min-height: 64px;
  padding: 12px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background: linear-gradient(115deg, #ffffff 0%, #f7faff 100%);
  border-bottom: 1px solid #e8edf5;
}
.ledger-toolbar__heading { display: flex; align-items: center; gap: 11px; min-width: 0; }
.ledger-toolbar__icon {
  width: 36px;
  height: 36px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 11px;
  background: #e9f2ff;
  color: #2463ac;
  font-size: 19px;
  font-weight: 700;
}
.ledger-toolbar__titles { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ledger-toolbar__titles strong { color: #172b4d; font-size: 15px; line-height: 1.1; letter-spacing: .015em; }
.ledger-toolbar__titles small { color: #73839a; font-size: 11px; white-space: nowrap; }
.ledger-toolbar__stats {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
  color: #526884;
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ledger-toolbar__selected {
  color: #255f9f;
  border: 1px solid #bdd7f7;
  background: #eaf3ff;
  padding: 4px 9px;
  border-radius: 999px;
}

/* 表头与数据行共用网格，保证金额列严格对齐。 */
.thead, .row {
  display: grid;
  align-items: center;
  gap: 6px;
  grid-template-columns: 20px 24px minmax(150px, 1.45fr) minmax(80px, .9fr)
                         minmax(118px, 1.05fr) minmax(86px, .67fr) minmax(86px, .67fr);
}
.thead {
  flex: none;
  min-height: 40px;
  padding: 9px 12px;
  font-size: 12px;
  font-weight: 650;
  letter-spacing: .02em;
  color: #5b6c84;
  border-bottom: 1px solid #dfe7f1;
  background: #f1f6fd;
}
.tbody {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 0;
  user-select: none;
  scrollbar-gutter: stable;
  scrollbar-color: #c5d2e3 transparent;
  scrollbar-width: thin;
}
.row {
  min-height: 38px;
  padding: 7px 12px;
  font-size: 13px;
  color: #25354b;
  border-bottom: 1px solid #edf1f6;
  background: #ffffff;
  transition: background-color .14s ease, box-shadow .14s ease;
}
.row--tx:nth-child(even) { background: #fcfdff; }
.row:hover { background: #f3f8ff; }
.row--node { font-weight: 650; background: #f7faff; }
.row--node:not(.row--root):not(.row--category) { box-shadow: inset 3px 0 0 #b4cbed; }
.row--root {
  min-height: 45px;
  color: #153e70;
  background: linear-gradient(90deg, #e5f0ff, #f1f7ff 80%);
  border-bottom-color: #d4e2f3;
  box-shadow: inset 4px 0 0 #2b6eb9;
}
.row--root .name { font-size: 14px; font-weight: 750; }
.row--root .c-num { font-size: 14px; font-weight: 750; }
.row--category {
  min-height: 41px;
  background: #f4f8fe;
  box-shadow: inset 3px 0 0 #91b8e9;
}
.row--category .name { color: #244d7d; font-weight: 700; }
.row--group { background: #f8f7fd; box-shadow: inset 3px 0 0 #a8a0da; }
.row--group .name { font-weight: 600; }
.row--sel, .row--sel:hover {
  background: #e6f1ff;
  box-shadow: inset 3px 0 0 #337bce;
}
.row--drag { opacity: .45; }
.row--drop, .row--drop:hover {
  outline: 2px solid #256fb9;
  outline-offset: -2px;
  background: #d7e9fe;
  box-shadow: inset 3px 0 0 #256fb9;
}
.c-grip { display: flex; justify-content: center; }
.grip {
  cursor: grab;
  color: #a4b3c8;
  user-select: none;
  padding: 0 2px;
  transition: color .14s;
}
.row:hover .grip { color: #5f7fa9; }
.c-check { display: flex; align-items: center; justify-content: center; }
.c-check input[type='checkbox'] {
  width: 14px;
  height: 14px;
  margin: 0;
  accent-color: #286cb6;
  cursor: pointer;
}
.c-name { display: flex; align-items: center; gap: 7px; min-width: 0; }
.c-name .name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: default;
}
.name--tx { font-weight: 450; }
.name--root { color: #183f6b; }
.c-note {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #72829a;
  font-size: 12px;
}
.c-meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #64748b;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.row--node .c-meta { font-weight: 650; }
.c-num {
  text-align: right;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 550;
  letter-spacing: -.01em;
  font-variant-numeric: tabular-nums;
}
.row--node .c-num, .row--group .c-num { font-weight: 700; }
.e { color: #cf3547; }
.i { color: #16865a; }
.twisty {
  width: 18px;
  height: 22px;
  flex: none;
  border: 0;
  border-radius: 5px;
  background: none;
  padding: 0;
  text-align: center;
  font-size: 15px;
  color: #6b85a8;
}
button.twisty:hover:not(:disabled) { background: #e1edfc; color: #255f9f; }
.inline-edit { width: 100%; min-width: 80px; }
.tag {
  flex: none;
  padding: 2px 8px;
  font-size: 10.5px;
  line-height: 1.3;
  border-radius: 999px;
  font-weight: 650;
  letter-spacing: .02em;
  background: #edf1f7;
  color: #586b84;
}
.tag--cat { color: #245d9c; background: #e1edfb; border: 1px solid #c9ddf6; }
.tag--net { color: #6955a6; background: #efebfc; border: 1px solid #ded5fa; }
.tag--m { color: #946316; background: #fff1d9; border: 1px solid #f6dfb7; }
.pad { padding: 16px; }

.muted { color: var(--color-muted); font-size: var(--font-sm, 13px); }
button {
  border: 1px solid var(--color-border); background: #fff; color: var(--color-text);
  border-radius: var(--radius-sm, 8px); padding: 4px 10px; cursor: pointer; font-size: 13px;
}
button:hover:not(:disabled) { background: #f0f4f9; }
button:disabled { opacity: .45; cursor: not-allowed; }
button.link { border: 0; background: none; padding: 0 4px; color: #1d5fa8; text-decoration: underline; }
.btn--primary { background: #1d5fa8; color: #fff; border-color: #1d5fa8; }
.btn--primary:hover:not(:disabled) { background: #174a83; }
input[type='text'], input:not([type]), input[type='date'], select {
  border: 1px solid var(--color-border); border-radius: var(--radius-sm, 8px); padding: 4px 8px; font-size: 13px;
  background: #fff; color: var(--color-text);
}
.banner { margin: 0; padding: 8px 12px; border-radius: var(--radius-sm, 8px); font-size: 13px; list-style: none; }
.banner--err { background: #fdecec; color: #b42318; }
.banner--ok { background: #e8f6ee; color: #1a7f4b; }
.banner--warn { background: #fff4e0; color: #9a5b00; }

.band { position: fixed; border: 1px solid #1d5fa8; background: rgba(29, 95, 168, .12); pointer-events: none; z-index: 60; }
.drag-hint {
  position: fixed; z-index: 80; pointer-events: none;
  background: #101828; color: #fff; font-size: 12px; padding: 4px 8px; border-radius: 6px; max-width: 320px;
}
.drag-hint--bad { background: #b42318; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(16, 24, 40, .35);
  display: flex; align-items: center; justify-content: center; z-index: 90;
}
.modal {
  background: #fff; border-radius: 12px; padding: 18px 20px; width: min(420px, 92vw);
  display: flex; flex-direction: column; gap: 10px; box-shadow: 0 12px 40px rgba(16, 24, 40, .25);
}
.modal h3 { margin: 0; font-size: 16px; }
.modal-input { width: 100%; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }

/* 窄屏：回到普通文档流，视图和内部分栏各自滚动。 */
@media (max-width: 900px) {
  .l2 { height: auto; overflow: visible; }
  .l2-layout { grid-template-columns: 1fr; }
  .l2-tabs { flex-direction: row; }
  .l2-tab { flex: 1; flex-direction: row; justify-content: center; padding: 9px; }
  .l2-main { height: 62vh; }
  .l2-side { overflow: visible; }
}
@media (max-width: 720px) {
  .l2-main { height: 58vh; }
}
</style>
