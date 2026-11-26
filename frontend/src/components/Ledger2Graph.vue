<script setup lang="ts">
/**
 * 账本工作台 · 星云视图。
 *
 * 只负责「画」和「视野」：布局、缩放（滚轮 / 按钮）、空白处拖框选择、中键拖视野。
 * 选择语义、按住拖动、放置合法性与写操作全在 Ledger2View（和表格视图同一套）。
 * 节点用 data-drop-key / data-drop-kind 标记，父组件拖放时靠 elementFromPoint 找目标。
 *
 * 布局是**动画**的：展开时新子节点从父节点位置长出来、周围的节点被挤到新扇区；
 * 折叠时子节点缩回父节点、其它节点挤回原位（d3 collapsible-tree 的同一套路：
 * 缓存上一帧位置 → 对目标位置做缓动，进入从父旧位置出发，退出朝父新位置收缩）。
 *
 * 相机「normal to」当前关心的对象：选择变化（单击 / 框选 / 筛选命中）取景到整个选区；
 * 展开 / 折叠时取景到「已选择 ∪ 刚变动的节点」。单个对象按层级放大
 * （交易 < 未展开的 M < 已展开的 M，已展开时缩放到刚好装下它的可见子节点），
 * 多个对象取它们可见子树的并集包围盒。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { Ledger2Group, Ledger2Node, Ledger2ReplayItem, Ledger2ReplayStep, Ledger2Tx } from '@/api/ledger2'
import type { ReplayPhase } from '@/composables/useLedger2Replay'
import { replayScene, replaySlots } from '@/utils/ledger2ReplayScene'

type Kind = 'node' | 'group' | 'tx'
type Role = 'primary' | 'mirror'

interface GNode {
  key: string
  kind: Kind
  name: string
  note: string
  e: number
  i: number
  count: number
  role?: Role
  hasChildren?: boolean
  depth: number
  x: number
  y: number
  r: number
  /** 这一条路径的颜色（色相 0–360）。同一个顶层分支下的 M 和 t 用同一个色相。 */
  hue: number
}
/** 屏幕上真正画的一个节点：目标信息 + 当前（动画中间态）位姿。 */
interface RNode extends GNode { o: number }
/** 布局前的条目：还没有位置和颜色。 */
type GItem = Omit<GNode, 'depth' | 'x' | 'y' | 'r' | 'hue'>
interface GEdge { id: string; from: string; to: string }
interface Graph { nodes: GNode[]; edges: GEdge[] }
interface Pose { x: number; y: number; r: number; o: number }

const props = defineProps<{
  tree: Ledger2Node | null
  expanded: Set<string>
  selected: Set<string>
  dragging: Set<string> | null
  dropTarget: string
  readOnly?: boolean
  replayStep?: Ledger2ReplayStep | null
  replayPhase?: ReplayPhase
}>()
const emit = defineEmits<{
  (e: 'item-down', ev: PointerEvent, key: string): void
  (e: 'band-select', keys: string[], additive: boolean): void
  (e: 'toggle', key: string): void
}>()

// ── 标签度量：布局要把「文字占的地方」算进去，不能只按圆点排 ───────────────

const yuan = (cents: number) => (cents / 100).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
/** 节点副标题用的紧凑金额：上万用「万」，否则整数元。 */
const shortYuan = (cents: number) => {
  const y = cents / 100
  const a = Math.abs(y)
  if (a >= 10000) {
    const w = y / 10000
    return (Math.abs(w) >= 10 ? w.toFixed(0) : w.toFixed(1)) + '万'
  }
  return y.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}
const valueLine = (it: { e: number; i: number; count: number }) =>
  `¥${shortYuan(it.e)} / ¥${shortYuan(it.i)} · ${it.count} 笔`
const money = (cents: number) => (Math.abs(cents) / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
/** 一笔交易的 IE 向量：支出 (−e, 0)，收入 (0, +i)，镜像腿刚好反过来。 */
const txIE = (n: { e: number; i: number }) => (n.e && n.i ? `−${money(n.e)} / +${money(n.i)}`
  : n.e ? `−${money(n.e)}` : `+${money(n.i)}`)

const CJK = /[\u2e80-\u9fff\uff00-\uffef\u3000-\u303f]/
const FS = 10.5                     // 标签字号（与 .l2g-tx / .l2g-tx-amt 一致）
const textContext = document.createElement('canvas').getContext('2d')
/** 估算一段文字的像素宽：CJK 按一个字宽，其余按 0.56 字宽。 */
function textW(s: string, size = FS): number {
  if (textContext) {
    textContext.font = `600 ${size}px "Segoe UI", "Microsoft YaHei", sans-serif`
    return textContext.measureText(s).width
  }
  let w = 0
  for (const ch of s) w += CJK.test(ch) ? size : size * 0.56
  return w
}
const LABEL_MAX = 88                // 名字最多这么宽
/** 过长的名字用省略号掐掉（布局和渲染用同一个结果，宽高才对得上）。 */
function clipText(s: string, maxW = LABEL_MAX): string {
  if (textW(s) <= maxW) return s
  let out = ''
  for (const ch of s) {
    if (textW(out + ch + '…') > maxW) break
    out += ch
  }
  return out + '…'
}
const radiusOfItem = (it: GItem): number => {
  if (it.kind === 'tx') return it.role === 'mirror' ? 4 : 5.5
  if (it.kind === 'group') return 12
  if (it.key === 'root') return 34
  return 9 + Math.min(14, Math.log2(it.count + 1) * 2.4)
}

// ── 标签碰撞：标题 + 金额都算进占位，压到了就把外侧那颗往外推 ─────────────
// 光按弧长分扇面不够：标签是从圆点往一侧横着伸出去的，左半边的标签会伸进
// 外一圈的节点里（右半边同理）。所以布局排完后按矩形再做几轮松弛。

interface Box { x0: number; y0: number; x1: number; y1: number }

/** 一条标签的宽度（标题 / 金额两行取宽者）。 */
function labelWidth(n: GNode, replay = false): number {
  if (replay) return textW(clipText(n.name, 130), 16)
  const name = clipText(n.name)
  return n.kind === 'tx'
    ? Math.max(textW(name), textW(txIE(n)))
    : Math.max(textW(name, 11.5), textW(valueLine(n), 10))
}
/** 节点连同标签一起占的矩形（世界坐标）。标签永远在 x 的正侧或负侧单边伸出。 */
function labelBox(n: GNode, replay = false): Box {
  if (n.key === 'root') {
    const half = replay ? Math.max(n.r, textW(n.name, 16) / 2) + 12
      : Math.max(textW(n.name, 14), textW(`支出 −¥${money(n.e)} · 收入 +¥${money(n.i)}`, 11)) / 2 + 12
    return { x0: n.x - half, x1: n.x + half, y0: n.y - n.r - 16, y1: n.y + 78 }
  }
  const gap = 12
  const radius = n.r + (n.kind === 'tx' ? 6 : 14)
  const w = labelWidth(n, replay) + gap + 12
  return n.x >= 0
    ? { x0: n.x - radius, x1: n.x + n.r + w, y0: n.y - Math.max(radius, 18), y1: n.y + Math.max(radius, 24) }
    : { x0: n.x - n.r - w, x1: n.x + radius, y0: n.y - Math.max(radius, 18), y1: n.y + Math.max(radius, 24) }
}

/** Reserve each label and halo before placing the next node. Moving outward keeps
 *  every branch in its own sector and terminates beyond the occupied bounds. */
function separateLabels(nodes: GNode[], replay = false) {
  const cells = new Map<string, Box[]>()
  const size = 128
  const keysFor = (b: Box) => {
    const keys: string[] = []
    for (let x = Math.floor(b.x0 / size); x <= Math.floor(b.x1 / size); x++) {
      for (let y = Math.floor(b.y0 / size); y <= Math.floor(b.y1 / size); y++) keys.push(`${x}:${y}`)
    }
    return keys
  }
  for (const node of [...nodes].sort((a, b) => a.depth - b.depth)) {
    const angle = Math.atan2(node.y, node.x)
    let box = labelBox(node, replay)
    while (keysFor(box).some(key => cells.get(key)?.some(other =>
      box.x0 < other.x1 && box.x1 > other.x0 && box.y0 < other.y1 && box.y1 > other.y0))) {
      node.x += Math.cos(angle) * 24
      node.y += Math.sin(angle) * 24
      box = labelBox(node, replay)
    }
    for (const key of keysFor(box)) {
      const entries = cells.get(key) ?? []
      entries.push(box)
      cells.set(key, entries)
    }
  }
}

// ── 布局：分支独立扇区 + 完整标签占位 ─────────────────────────────

const liveGraph = computed<Graph>(() => {
  const t = props.tree
  if (!t) return { nodes: [], edges: [] }

  const nodeItem = (n: Ledger2Node): GItem => ({
    key: n.id, kind: 'node', name: n.name, note: n.note, e: n.e, i: n.i, count: n.count,
    hasChildren: n.children.length > 0,
  })
  const groupItem = (g: Ledger2Group): GItem => ({
    key: `g:${g.id}`, kind: 'group', name: g.name || '（未命名净额组）', note: g.note,
    e: g.e, i: g.i, count: g.count, hasChildren: g.members.length > 0,
  })
  const txItem = (x: Ledger2Tx): GItem => ({
    key: x.ref, kind: 'tx', name: x.title || x.counterparty || x.item || `#${x.tx_id}`,
    note: x.memo, e: x.e, i: x.i, count: 1, role: x.role,
  })

  const nodeMap = new Map<string, Ledger2Node>()
  const groupMap = new Map<string, Ledger2Group>()
  const walk = (n: Ledger2Node) => {
    nodeMap.set(n.id, n)
    for (const c of n.children) {
      if (c.type === 'node') walk(c)
      else if (c.type === 'group') groupMap.set(c.id, c)
    }
  }
  walk(t)

  function childrenOf(key: string, kind: Kind): GItem[] {
    if (kind === 'tx' || !props.expanded.has(kind === 'group' ? key.slice(2) : key)) return []
    if (kind === 'group') {
      const g = groupMap.get(key.slice(2))
      return g ? g.members.map(txItem) : []
    }
    const n = nodeMap.get(key)
    if (!n) return []
    return n.children.map(c => (c.type === 'node' ? nodeItem(c) : c.type === 'group' ? groupItem(c) : txItem(c)))
  }

  return layoutStars(nodeItem(t), item => childrenOf(item.key, item.kind))
})

function layoutStars(root: GItem, childrenOf: (item: GItem) => GItem[], replay = false): Graph {
  const nodes: GNode[] = []
  const edges: GEdge[] = []
  // 楔形按**可见叶数**分，而不是按交易数：d3 collapsible tree 的做法。
  // 这样展开一个节点会撑大它的楔形、把同一圈的邻居挤开；折叠后又把楔形还回去。
  const leavesCache = new Map<string, number>()
  function leavesOf(it: GItem): number {
    const hit = leavesCache.get(it.key)
    if (hit !== undefined) return hit
    leavesCache.set(it.key, 1)          // 防环
    const kids = childrenOf(it)
    let n = 1
    if (kids.length) {
      n = 0
      for (const k of kids) n += leavesOf(k)
    }
    leavesCache.set(it.key, n)
    return n
  }
  // All descendants share their top-level branch's angular sector. Weighting
  // by visible leaves reserves space for expanded groups before any points move.
  const palette = [204, 163, 38, 278, 338, 186, 92, 24]
  const leafCount = leavesOf(root)
  const ringStep = replay ? 138 : Math.max(180, Math.min(420, Math.sqrt(leafCount) * 31))
  function layout(it: GItem, depth: number, start: number, end: number, hue: number) {
    const angle = (start + end) / 2
    const radius = depth === 0 ? 0 : (replay ? 165 : 240) + (depth - 1) * ringStep
    const node: GNode = {
      ...it, depth, x: Math.cos(angle) * radius, y: Math.sin(angle) * radius,
      r: radiusOfItem(it), hue,
    }
    nodes.push(node)
    const kids = childrenOf(it)
    const weight = (child: GItem) => depth === 0 ? Math.sqrt(leavesOf(child)) : leavesOf(child)
    const total = kids.reduce((sum, child) => sum + weight(child), 0)
    let cursor = start
    for (const [index, child] of kids.entries()) {
      const span = (end - start) * weight(child) / total
      edges.push({ id: `${it.key}->${child.key}`, from: it.key, to: child.key })
      layout(child, depth + 1, cursor, cursor + span, depth === 0 ? palette[index % palette.length] : hue)
      cursor += span
    }
  }
  layout(root, 0, -Math.PI / 2, Math.PI * 1.5, 204)
  separateLabels(nodes, replay)
  return { nodes, edges }
}

const replayPlan = computed(() => {
  if (!props.replayStep) return null
  const { before, after, changed, targets, bundles } = replayScene(props.replayStep)
  const { slots, beforeKeys, afterKeys } = replaySlots(before, after)
  const virtualRoot: GItem = { key: '@replay', kind: 'node', name: '', note: '', e: 0, i: 0, count: 0 }
  const branches = new Map<string, GItem[]>()
  for (const [key, { item, parent }] of slots) {
    const owner = parent ?? virtualRoot.key
    const children = branches.get(owner) ?? []
    const role = item.kind === 'tx' ? (item.ref.startsWith('txm:') ? 'mirror' : 'primary') : undefined
    children.push({ key, kind: item.kind, name: item.name, note: item.note, e: 0, i: 0, count: 0, role })
    branches.set(owner, children)
  }
  const roots = branches.get(virtualRoot.key) ?? []
  const layout = layoutStars(roots.length === 1 ? roots[0] : virtualRoot, item => branches.get(item.key) ?? [], true)
  const posesBySlot = new Map(layout.nodes.map(node => [node.key, node]))

  function snapshot(items: Map<string, Ledger2ReplayItem>, keys: Map<string, string>): Graph {
    const nodes: GNode[] = []
    const edges: GEdge[] = []
    for (const item of items.values()) {
      const placement = posesBySlot.get(keys.get(item.ref)!)!
      nodes.push({ ...placement, key: item.ref, name: item.name, note: item.note })
      if (item.parent && items.has(item.parent)) edges.push({ id: `${item.parent}->${item.ref}`, from: item.parent, to: item.ref })
    }
    return { nodes, edges }
  }

  const revealed = new Map(before)
  const revealedKeys = new Map(beforeKeys)
  for (const [ref, item] of after) {
    if (revealed.has(ref)) continue
    revealed.set(ref, item)
    revealedKeys.set(ref, afterKeys.get(ref)!)
  }
  const completed = new Map(after)
  const completedKeys = new Map(afterKeys)
  for (const [ref, item] of before) {
    if (completed.has(ref) || changed.has(ref)) continue
    completed.set(ref, item)
    completedKeys.set(ref, beforeKeys.get(ref)!)
  }
  const frames = {
    source: snapshot(before, beforeKeys), target: snapshot(revealed, revealedKeys),
    result: snapshot(completed, completedKeys),
  }
  return { frames, slots: layout.nodes.filter(node => node.key !== virtualRoot.key), changed, targets, bundles }
})

const graph = computed(() => replayPlan.value?.frames[props.replayPhase ?? 'source'] ?? liveGraph.value)

// ── 动画：目标位置 → 当前位姿（d3 collapsible tree 的套路）─────────────────

const poses = new Map<string, Pose>()
const animPhase = ref(0)
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3)
const lerp = (a: number, b: number, t: number) => a + (b - a) * t

let anim: {
  t0: number
  dur: number
  tracks: Map<string, { from: Pose; to: Pose }>
  ghosts: Map<string, { meta: GNode; from: Pose; to: Pose; fromKey: string | null }>
  final: GNode[]
} | null = null
let animRaf = 0
let prevParent = new Map<string, string>()
let prevMeta = new Map<string, GNode>()

function startAnimation() {
  if (animRaf) cancelAnimationFrame(animRaf)
  const step = (ts: number) => {
    const a = anim
    if (!a) { animRaf = 0; return }
    const t = Math.min(1, (ts - a.t0) / a.dur)
    const k = easeOut(t)
    const blend = (from: Pose, to: Pose): Pose => ({
      x: lerp(from.x, to.x, k), y: lerp(from.y, to.y, k),
      r: lerp(from.r, to.r, k), o: lerp(from.o, to.o, k),
    })
    for (const [key, tr] of a.tracks) poses.set(key, blend(tr.from, tr.to))
    for (const [key, gh] of a.ghosts) poses.set(key, blend(gh.from, gh.to))
    animPhase.value++
    if (t < 1) {
      animRaf = requestAnimationFrame(step)
    } else {
      for (const n of a.final) poses.set(n.key, { x: n.x, y: n.y, r: n.r, o: 1 })
      for (const key of a.ghosts.keys()) poses.delete(key)
      anim = null
      animRaf = 0
      animPhase.value++
    }
  }
  animRaf = requestAnimationFrame(step)
}

function rebuild(next: { nodes: GNode[]; edges: GEdge[] }) {
  const target = new Map(next.nodes.map(n => [n.key, n]))
  const parent = new Map<string, string>()
  for (const e of next.edges) parent.set(e.to, e.from)
  if (props.replayStep && !prevMeta.size) {
    for (const node of next.nodes) poses.set(node.key, { x: node.x, y: node.y, r: node.r, o: 1 })
    prevParent = parent
    prevMeta = target
    return
  }

  const tracks = new Map<string, { from: Pose; to: Pose }>()
  for (const n of next.nodes) {
    const cur = poses.get(n.key)
    const to: Pose = { x: n.x, y: n.y, r: n.r, o: 1 }
    if (cur) {
      if (!props.replayStep || cur.x !== to.x || cur.y !== to.y || cur.r !== to.r || cur.o !== 1) {
        tracks.set(n.key, { from: { ...cur }, to })
      }
    } else {
      // 新出现的节点（展开 / 首次加载）：从父节点此刻的位置长出来
      const pk = parent.get(n.key) ?? prevParent.get(n.key) ?? ''
      const base = poses.get(pk) ?? (pk ? target.get(pk) : undefined)
      tracks.set(n.key, { from: { x: base?.x ?? n.x, y: base?.y ?? n.y, r: 0, o: 0 }, to })
    }
  }

  // 消失的节点（折叠）：朝父节点的新位置缩回去，缩到 0 再移除
  const ghosts = new Map<string, { meta: GNode; from: Pose; to: Pose; fromKey: string | null }>()
  for (const [key, p] of poses) {
    if (target.has(key)) continue
    const meta = prevMeta.get(key)
    if (!meta) continue
    const pk = prevParent.get(key) ?? ''
    const base = (pk ? target.get(pk) : undefined) ?? poses.get(pk)
    ghosts.set(key, {
      meta, from: { ...p }, to: { x: base?.x ?? p.x, y: base?.y ?? p.y, r: 0, o: 0 },
      fromKey: pk || null,
    })
  }

  prevParent = parent
  prevMeta = new Map(next.nodes.map(n => [n.key, n]))
  if (!tracks.size && !ghosts.size) return
  anim = { t0: performance.now(), dur: 420, tracks, ghosts, final: next.nodes }
  startAnimation()
}

watch(graph, g => rebuild(g), { immediate: true })

const renderNodes = computed<RNode[]>(() => {
  void animPhase.value
  const out: RNode[] = []
  for (const n of graph.value.nodes) {
    const p = poses.get(n.key) ?? { x: n.x, y: n.y, r: n.r, o: 1 }
    out.push({ ...n, x: p.x, y: p.y, r: p.r, o: p.o })
  }
  if (anim) {
    for (const [key, gh] of anim.ghosts) {
      const p = poses.get(key)
      if (p && p.o > 0.02) out.push({ ...gh.meta, x: p.x, y: p.y, r: p.r, o: p.o })
    }
  }
  return out
})

const renderEdges = computed<{ id: string; a: Pose; b: Pose; o: number }[]>(() => {
  void animPhase.value
  const out: { id: string; a: Pose; b: Pose; o: number }[] = []
  for (const e of graph.value.edges) {
    const a = poses.get(e.from)
    const b = poses.get(e.to)
    if (a && b) out.push({ id: e.id, a, b, o: Math.min(a.o, b.o) })
  }
  if (anim) {
    for (const [key, gh] of anim.ghosts) {
      const a = gh.fromKey ? poses.get(gh.fromKey) : undefined
      const b = poses.get(key)
      if (a && b) out.push({ id: `ghost:${key}`, a, b, o: b.o })
    }
  }
  return out
})

function pathOf(a: Pose, b: Pose): string {
  const distance = Math.hypot(b.x - a.x, b.y - a.y)
  if (!distance) return ''
  const ux = (b.x - a.x) / distance
  const uy = (b.y - a.y) / distance
  const start = { x: a.x + ux * (a.r + 4), y: a.y + uy * (a.r + 4) }
  const end = { x: b.x - ux * (b.r + 4), y: b.y - uy * (b.r + 4) }
  const midRadius = (Math.hypot(a.x, a.y) + Math.hypot(b.x, b.y)) / 2
  const angleA = Math.atan2(a.y, a.x)
  const angleB = Math.atan2(b.y, b.x)
  return `M ${start.x} ${start.y} C ${Math.cos(angleA) * midRadius} ${Math.sin(angleA) * midRadius}, ${Math.cos(angleB) * midRadius} ${Math.sin(angleB) * midRadius}, ${end.x} ${end.y}`
}

const txTotal = computed(() => graph.value.nodes.filter(n => n.kind === 'tx').length)
const hoverKey = ref('')
const activeKey = computed(() => hoverKey.value || (!props.replayStep && props.selected.size === 1 ? [...props.selected][0] : ''))
const replayEdges = computed(() => {
  const plan = replayPlan.value
  if (!plan) return new Set<string>()
  return new Set(graph.value.edges.filter(edge => plan.changed.has(edge.to) || plan.targets.has(edge.to)).map(edge => edge.id))
})
const replayCaption = computed(() => {
  if (!replayPlan.value?.changed.size) return '本步暂无可显示的节点变化'
  if (props.replayPhase === 'target') return '目标位置 · 青色星点是目标分组'
  if (props.replayPhase === 'result') return '操作结果 · 连线显示新的归属'
  return '原位置 · 沿连线可查看父节点'
})
const replayMembersOpen = ref(false)
const relatedEdges = computed(() => {
  const result = new Set<string>()
  const parent = new Map(graph.value.edges.map(edge => [edge.to, edge]))
  let cursor = activeKey.value
  while (parent.has(cursor)) {
    const edge = parent.get(cursor)!
    result.add(edge.id)
    cursor = edge.from
  }
  for (const edge of graph.value.edges) if (edge.from === activeKey.value) result.add(edge.id)
  return result
})
const relatedNodes = computed(() => {
  const result = new Set([activeKey.value])
  for (const edge of graph.value.edges) {
    if (relatedEdges.value.has(edge.id)) { result.add(edge.from); result.add(edge.to) }
  }
  return result
})
/** 金额是认交易的唯一依据：一律显示。名字只在放得下时显示（少了 / 放大了 / 选中的）。 */
const showTxName = (key: string) =>
  !!props.replayStep || props.selected.has(key) || hoverKey.value === key || txTotal.value <= 24 || zoom.value >= 2.2
const isOpen = (n: { key: string; kind: Kind }) => props.expanded.has(n.kind === 'group' ? n.key.slice(2) : n.key)

// ── 视野：缩放 / 平移 / normal-to ─────────────────────────────────────────

const svgEl = ref<SVGSVGElement | null>(null)
const sceneEl = ref<SVGGElement | null>(null)
const zoom = ref(1.15)
const pan = ref({ x: 0, y: 0 })
const ZOOM_MIN = 0.2
const ZOOM_MAX = 4
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

function svgPoint(e: { clientX: number; clientY: number }) {
  const svg = svgEl.value
  if (!svg) return { x: 0, y: 0 }
  const pt = svg.createSVGPoint()
  pt.x = e.clientX
  pt.y = e.clientY
  const m = svg.getScreenCTM()
  if (!m) return { x: 0, y: 0 }
  const p = pt.matrixTransform(m.inverse())
  return { x: p.x, y: p.y }
}
function worldPoint(e: { clientX: number; clientY: number }) {
  const svg = svgEl.value
  const scene = sceneEl.value
  if (!svg || !scene) return { x: 0, y: 0 }
  const pt = svg.createSVGPoint()
  pt.x = e.clientX
  pt.y = e.clientY
  const m = scene.getScreenCTM()
  if (!m) return { x: 0, y: 0 }
  const p = pt.matrixTransform(m.inverse())
  return { x: p.x, y: p.y }
}

let camRaf = 0
function cancelCamera() {
  if (camRaf) {
    cancelAnimationFrame(camRaf)
    camRaf = 0
  }
}
/** 相机缓动：zoom 与 pan 同时插值（pan 以 viewBox 原点为中心计算，见 focusOn）。 */
function animateCamera(z1: number, p1: { x: number; y: number }, dur = 460) {
  cancelCamera()
  const z0 = zoom.value
  const p0 = { ...pan.value }
  if (Math.abs(z1 - z0) < 0.001 && Math.abs(p1.x - p0.x) < 0.5 && Math.abs(p1.y - p0.y) < 0.5) return
  const t0 = performance.now()
  const step = (ts: number) => {
    const t = Math.min(1, (ts - t0) / dur)
    const k = 1 - Math.pow(1 - t, 3)
    zoom.value = z0 + (z1 - z0) * k
    pan.value = { x: p0.x + (p1.x - p0.x) * k, y: p0.y + (p1.y - p0.y) * k }
    if (t < 1) camRaf = requestAnimationFrame(step)
    else camRaf = 0
  }
  camRaf = requestAnimationFrame(step)
}

function onWheel(e: WheelEvent) {
  cancelCamera()
  const next = clamp(zoom.value * (e.deltaY < 0 ? 1.1 : 0.9), ZOOM_MIN, ZOOM_MAX)
  const p = svgPoint(e)
  const w = worldPoint(e)
  pan.value = { x: p.x - w.x * next, y: p.y - w.y * next }
  zoom.value = next
}
function zoomBy(f: number) {
  cancelCamera()
  const svg = svgEl.value
  const next = clamp(zoom.value * f, ZOOM_MIN, ZOOM_MAX)
  if (!svg) {
    zoom.value = next
    return
  }
  const box = svg.viewBox.baseVal
  const cx = box.x + box.width / 2
  const cy = box.y + box.height / 2
  const w = { x: (cx - pan.value.x) / zoom.value, y: (cy - pan.value.y) / zoom.value }
  pan.value = { x: cx - w.x * next, y: cy - w.y * next }
  zoom.value = next
}
function resetCamera() {
  if (replayPlan.value) fitReplay()
  else focusOn('root')
}

const VIEW_W = 1800
const VIEW_H = 1000

function fitReplay() {
  const slots = replayPlan.value?.slots ?? []
  if (!slots.length) return
  const box = slots.map(node => labelBox(node, true)).reduce((bounds, item) => ({
    x0: Math.min(bounds.x0, item.x0), x1: Math.max(bounds.x1, item.x1),
    y0: Math.min(bounds.y0, item.y0), y1: Math.max(bounds.y1, item.y1),
  }))
  cancelCamera()
  zoom.value = clamp(Math.min(VIEW_W / (box.x1 - box.x0 + 170), VIEW_H / (box.y1 - box.y0 + 140)), ZOOM_MIN, 2.5)
  pan.value = { x: -zoom.value * (box.x0 + box.x1) / 2, y: -zoom.value * (box.y0 + box.y1) / 2 }
}

watch(replayPlan, fitReplay, { immediate: true })

function childMap(): Map<string, string[]> {
  const kids = new Map<string, string[]>()
  for (const e of graph.value.edges) {
    const arr = kids.get(e.from)
    if (arr) arr.push(e.to)
    else kids.set(e.from, [e.to])
  }
  return kids
}

/** 一个节点连同它当前可见的子树占的矩形。 */
function subtreeBox(n: GNode, idx: Map<string, GNode>, kids: Map<string, string[]>): Box {
  let x0 = n.x - n.r, x1 = n.x + n.r, y0 = n.y - n.r, y1 = n.y + n.r
  const stack = [n.key]
  const seen = new Set<string>()
  while (stack.length) {
    const k = stack.pop() as string
    if (seen.has(k)) continue
    seen.add(k)
    const item = idx.get(k)
    if (!item) continue
    const bounds = labelBox(item)
    x0 = Math.min(x0, bounds.x0)
    x1 = Math.max(x1, bounds.x1)
    y0 = Math.min(y0, bounds.y0)
    y1 = Math.max(y1, bounds.y1)
    for (const c of kids.get(k) ?? []) stack.push(c)
  }
  return { x0, y0, x1, y1 }
}

/** 一个节点「该放多大、以哪为中心」：交易 < 未展开的 M < 已展开的 M。
 *
 * 已展开的 M 要「刚好装下它的可见子树」，所以取子树包围盒的**中心**（不是节点自己），
 * 否则子节点全在一边时会被画布边缘切掉。子树太大时允许缩到 1 以下。 */
function focusTarget(n: GNode): { z: number; cx: number; cy: number } {
  const idx = new Map(graph.value.nodes.map(x => [x.key, x]))
  const box = subtreeBox(n, idx, childMap())
  const bw = Math.max(140, box.x1 - box.x0 + 120)   // 给条目右侧的金额标签留余量
  const bh = Math.max(140, box.y1 - box.y0 + 100)
  const fit = Math.min(VIEW_W / bw, VIEW_H / bh)
  if (n.kind === 'tx') return { z: clamp(fit, 1.2, 1.9), cx: n.x, cy: n.y }
  if (n.key === 'root') return { z: clamp(fit, ZOOM_MIN, 1.3), cx: (box.x0 + box.x1) / 2, cy: (box.y0 + box.y1) / 2 }
  if (!isOpen(n)) return { z: clamp(fit, 2.1, 2.6), cx: n.x, cy: n.y }
  return { z: clamp(fit, ZOOM_MIN, 3.4), cx: (box.x0 + box.x1) / 2, cy: (box.y0 + box.y1) / 2 }
}

function focusOn(key: string) {
  const n = graph.value.nodes.find(x => x.key === key)
  if (!n) return
  const { z, cx, cy } = focusTarget(n)
  // viewBox 以 (0,0) 为中心：translate(pan) scale(z) 下，把世界点 (cx,cy) 放到原点
  animateCamera(z, { x: -z * cx, y: -z * cy })
}

/** 一组对象一起取景：把它们的可见子树并成一个包围盒。 */
function fitKeys(keys: Iterable<string>): boolean {
  const idx = new Map(graph.value.nodes.map(x => [x.key, x]))
  const kids = childMap()
  let box: Box | null = null
  for (const key of new Set(keys)) {
    const n = idx.get(key)
    if (!n) continue
    const b = subtreeBox(n, idx, kids)
    box = box ? {
      x0: Math.min(box.x0, b.x0), y0: Math.min(box.y0, b.y0),
      x1: Math.max(box.x1, b.x1), y1: Math.max(box.y1, b.y1),
    } : b
  }
  if (!box) return false
  const bw = Math.max(140, box.x1 - box.x0 + 120)
  const bh = Math.max(140, box.y1 - box.y0 + 100)
  const z = clamp(Math.min(VIEW_W / bw, VIEW_H / bh), ZOOM_MIN, 3.4)
  animateCamera(z, { x: -z * (box.x0 + box.x1) / 2, y: -z * (box.y0 + box.y1) / 2 })
  return true
}

/** 单个对象沿用分层取景；多个对象取并集。已折叠、看不见的键自动忽略。 */
function normalTo(keys: Iterable<string>) {
  const visible = [...new Set(keys)].filter(key => graph.value.nodes.some(n => n.key === key))
  if (!visible.length) return
  if (visible.length === 1) focusOn(visible[0])
  else fitKeys(visible)
}

// 选择一变（单击、框选、筛选命中）就取景到整个选区。
watch(() => props.selected, next => {
  if (!props.replayStep && next.size) normalTo(next)
}, { immediate: true })

// 展开/折叠任一节点时，取景到「已选择 ∪ 刚变动的那个」；没有选择就只看变动项。
let lastExpanded = new Set(props.expanded)
watch(() => props.expanded, next => {
  if (props.replayStep) return
  const changed: string[] = []
  for (const id of next) if (!lastExpanded.has(id)) changed.push(id)
  for (const id of lastExpanded) if (!next.has(id)) changed.push(id)
  lastExpanded = new Set(next)
  if (!changed.length) return
  // expanded 存的是节点 / 净额组 id；星图里净额组的键多一层 g: 前缀。
  normalTo(new Set([...props.selected, ...changed.flatMap(id => [id, `g:${id}`])]))
})

// ── 空白处：拖框选择；按住不动 = 拖视野；中键按下拖 = 拖视野 ──────────────

const BAND_MIN = 4
const LONG_PRESS_MS = 280
const band = ref<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
let bg: {
  x: number; y: number; sx: number; sy: number
  panX: number; panY: number
  moved: boolean; panning: boolean; additive: boolean
} | null = null
let bgTimer: number | null = null

function bgDown(e: PointerEvent) {
  if (e.button === 1) {
    // 中键（轮键）按住拖 = 移动视野，和拖动滚轮一样
    e.preventDefault()
    cancelCamera()
    const p = svgPoint(e)
    bg = {
      x: e.clientX, y: e.clientY, sx: p.x, sy: p.y,
      panX: pan.value.x, panY: pan.value.y,
      moved: true, panning: true, additive: false,
    }
    window.addEventListener('pointermove', bgMove)
    window.addEventListener('pointerup', bgUp)
    window.addEventListener('pointercancel', bgUp)
    return
  }
  if (e.button !== 0 || props.readOnly) return
  cancelCamera()
  const p = svgPoint(e)
  bg = {
    x: e.clientX, y: e.clientY, sx: p.x, sy: p.y,
    panX: pan.value.x, panY: pan.value.y,
    moved: false, panning: false, additive: e.ctrlKey || e.metaKey,
  }
  bgTimer = window.setTimeout(() => {
    bgTimer = null
    if (bg && !bg.moved) {
      bg.panning = true
      band.value = null
    }
  }, LONG_PRESS_MS)
  window.addEventListener('pointermove', bgMove)
  window.addEventListener('pointerup', bgUp)
  window.addEventListener('pointercancel', bgUp)
}

function bgMove(e: PointerEvent) {
  if (!bg) return
  if (!bg.moved && Math.hypot(e.clientX - bg.x, e.clientY - bg.y) > BAND_MIN) {
    bg.moved = true
    if (bgTimer !== null) {
      clearTimeout(bgTimer)
      bgTimer = null
    }
  }
  if (bg.panning) {
    const p = svgPoint(e)
    pan.value = { x: bg.panX + (p.x - bg.sx), y: bg.panY + (p.y - bg.sy) }
  } else if (bg.moved) {
    band.value = { x0: bg.x, y0: bg.y, x1: e.clientX, y1: e.clientY }
  }
}

function bgUp() {
  if (bgTimer !== null) {
    clearTimeout(bgTimer)
    bgTimer = null
  }
  window.removeEventListener('pointermove', bgMove)
  window.removeEventListener('pointerup', bgUp)
  window.removeEventListener('pointercancel', bgUp)
  const b = band.value
  const state = bg
  band.value = null
  bg = null
  if (!state || state.panning) return
  if (b) {
    const left = Math.min(b.x0, b.x1)
    const right = Math.max(b.x0, b.x1)
    const top = Math.min(b.y0, b.y1)
    const bottom = Math.max(b.y0, b.y1)
    if (right - left >= BAND_MIN || bottom - top >= BAND_MIN) {
      const keys: string[] = []
      for (const el of svgEl.value?.querySelectorAll<SVGGElement>('[data-drop-key]') ?? []) {
        const key = el.dataset.dropKey || ''
        if (!key || key === 'root') continue
        const r = el.getBoundingClientRect()
        if (r.bottom > top && r.top < bottom && r.right > left && r.left < right && !keys.includes(key)) keys.push(key)
      }
      emit('band-select', keys, state.additive)
      return
    }
  }
  if (!state.moved && !state.additive) emit('band-select', [], false)
}

const bandStyle = computed(() => {
  const b = band.value
  if (!b) return {}
  return {
    left: `${Math.min(b.x0, b.x1)}px`, top: `${Math.min(b.y0, b.y1)}px`,
    width: `${Math.abs(b.x1 - b.x0)}px`, height: `${Math.abs(b.y1 - b.y0)}px`,
  }
})

function nodeDown(e: PointerEvent, n: GNode) {
  if (e.button !== 0) return
  e.stopPropagation()
  if (!props.readOnly) emit('item-down', e, n.key)
}

function toggleNode(key: string) {
  if (!props.readOnly) emit('toggle', key)
}

onBeforeUnmount(() => {
  if (bgTimer !== null) clearTimeout(bgTimer)
  if (animRaf) cancelAnimationFrame(animRaf)
  cancelCamera()
  window.removeEventListener('pointermove', bgMove)
  window.removeEventListener('pointerup', bgUp)
  window.removeEventListener('pointercancel', bgUp)
})
</script>

<template>
  <div class="l2g" :class="{ 'is-replaying': replayStep }" data-testid="graph" :data-replay-phase="replayStep ? replayPhase : undefined">
    <svg ref="svgEl" class="l2g-svg" viewBox="-900 -500 1800 1000"
         @pointerdown="bgDown" @wheel.prevent="onWheel"
         @mousedown.middle.prevent @auxclick.prevent>
      <defs>
        <pattern id="l2gField" width="110" height="105" patternUnits="userSpaceOnUse">
          <circle cx="16" cy="21" r=".65" fill="#8cadde" opacity=".19" />
          <circle cx="82" cy="74" r=".5" fill="#fff" opacity=".12" />
        </pattern>
        <radialGradient id="l2gCore">
          <stop offset="0%" stop-color="#4d6cff" stop-opacity=".19" />
          <stop offset="100%" stop-color="#0b1226" stop-opacity="0" />
        </radialGradient>
      </defs>
      <rect x="-900" y="-500" width="1800" height="1000" fill="#0b1226" />
      <rect x="-900" y="-500" width="1800" height="1000" fill="url(#l2gField)" />
      <g ref="sceneEl" :transform="`translate(${pan.x} ${pan.y}) scale(${zoom})`">
        <circle r="230" fill="url(#l2gCore)" pointer-events="none" />
        <g class="l2g-edges" pointer-events="none">
          <path v-for="e in renderEdges" :key="e.id" :d="pathOf(e.a, e.b)" :opacity="e.o" :data-edge="e.id"
                :class="{ 'is-related': relatedEdges.has(e.id) || replayEdges.has(e.id), 'is-muted': activeKey && !relatedEdges.has(e.id) }" />
        </g>
        <g v-for="n in renderNodes" :key="n.key" class="l2g-node"
           :class="[`kind-${n.kind}`, `role-${n.role ?? 'none'}`, {
             root: n.key === 'root',
             'is-muted': activeKey && !relatedNodes.has(n.key),
             selected: replayPlan ? replayPlan.changed.has(n.key) : selected.has(n.key),
             'replay-target': replayPhase !== 'source' && replayPlan?.targets.has(n.key),
             dragging: dragging?.has(n.key) ?? false,
             'drop-target': dropTarget === n.key,
           }]"
           :opacity="n.o"
           :transform="`translate(${n.x} ${n.y})`"
           :style="{ '--h': Math.round(n.hue) }"
           :data-drop-key="n.key" :data-drop-kind="n.kind"
           @pointerdown="nodeDown($event, n)"
           @pointerenter="hoverKey = n.key" @pointerleave="hoverKey = ''"
           @dblclick.stop="toggleNode(n.key)">
          <title>{{ n.name }}{{ n.note ? ` · ${n.note}` : '' }}{{ replayStep ? '' : ` · ¥${yuan(n.kind === 'tx' ? n.e || n.i : n.e + n.i)}` }}</title>
          <circle :r="Math.max(n.r + 8, 14)" class="hit" />
          <circle :r="n.r + (n.kind === 'tx' ? 3 : 9)" class="halo" />
          <circle :r="n.r" class="core" />
          <text v-if="n.key === 'root'" y="4" text-anchor="middle" class="l2g-root">{{ n.name }}</text>
          <text v-if="n.key === 'root' && !replayStep" y="56" text-anchor="middle" class="l2g-root-amt">
           支出 −¥{{ money(n.e) }} · 收入 +¥{{ money(n.i) }}
          </text>
          <template v-else-if="n.key !== 'root'">
            <text v-if="n.kind !== 'tx'" :x="n.x >= 0 ? n.r + 12 : -(n.r + 12)"
                  :text-anchor="n.x >= 0 ? 'start' : 'end'" y="-1" class="l2g-label">{{ replayStep ? clipText(n.name, 130) : clipText(n.name) }}</text>
            <text v-if="!replayStep && n.kind !== 'tx' && zoom >= 1.4" :x="n.x >= 0 ? n.r + 12 : -(n.r + 12)"
                  :text-anchor="n.x >= 0 ? 'start' : 'end'" y="12" class="l2g-value">
              {{ valueLine(n) }}
            </text>
            <template v-if="n.kind === 'tx'">
              <text v-if="showTxName(n.key)" :x="n.x >= 0 ? n.r + 10 : -(n.r + 10)"
                    :text-anchor="n.x >= 0 ? 'start' : 'end'" y="-2" class="l2g-tx">{{ replayStep ? clipText(n.name, 130) : clipText(n.name) }}</text>
              <text v-if="!replayStep" :x="n.x >= 0 ? n.r + 10 : -(n.r + 10)" :text-anchor="n.x >= 0 ? 'start' : 'end'"
                    :y="showTxName(n.key) ? 13 : 3" class="l2g-tx-amt">{{ txIE(n) }}</text>
            </template>
          </template>
          <g v-if="!readOnly && n.hasChildren && n.o > 0.9" :transform="`translate(${n.r * 0.75} ${-n.r * 0.75})`"
             class="l2g-more" :class="{ 'is-open': isOpen(n) }"
             @pointerdown.stop @click.stop="toggleNode(n.key)">
            <circle r="7" />
            <text y="4" text-anchor="middle">{{ isOpen(n) ? '−' : '+' }}</text>
          </g>
        </g>
      </g>
    </svg>

    <div class="l2g-zoom">
      <button title="放大" @click="zoomBy(1.2)">＋</button>
      <button title="缩小" @click="zoomBy(1 / 1.2)">－</button>
      <button title="适应全部可见节点" @click="resetCamera">复位</button>
      <span>{{ Math.round(zoom * 100) }}%</span>
    </div>
    <details v-if="replayPlan?.bundles.length" class="replay-members"
             @toggle="replayMembersOpen = ($event.target as HTMLDetailsElement).open">
      <summary>查看合并展示的交易</summary>
      <div v-if="replayMembersOpen" class="replay-members-list">
        <section v-for="bundle in replayPlan.bundles" :key="bundle.key">
          <strong>{{ bundle.from }} → {{ bundle.to }}</strong>
          <ol><li v-for="(name, index) in bundle.names" :key="index">{{ name }}</li></ol>
        </section>
      </div>
    </details>
    <p v-if="replayStep" class="l2g-help replay-caption" data-testid="replay-caption">{{ replayCaption }}</p>
    <p v-else-if="readOnly" class="l2g-help">正在读取回放</p>
    <p v-else class="l2g-help">滚轮缩放 · 中键拖视野 · 空白拖框选择 · 选中后按住任意条目拖动 · 双击或 ± 展开/折叠</p>
    <div v-if="band" class="l2g-band" :style="bandStyle" />
  </div>
</template>

<style scoped>
.l2g {
  position: relative; flex: 1 1 auto; min-height: 0; height: auto; overflow: hidden;
  background: #0b1226; border: 1px solid var(--color-border); border-radius: var(--radius);
}
.l2g-svg { display: block; width: 100%; height: 100%; touch-action: none; user-select: none; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
.is-replaying .l2g-label, .is-replaying .l2g-tx { font-size: 16px; }
.is-replaying .l2g-node.replay-target .core { stroke: #70e0da; stroke-width: 2.5; }
.is-replaying .replay-caption { font-size: 12px; color: #b8d5fb; }
.replay-members { position: absolute; top: 12px; left: 12px; max-width: min(320px, 65%); padding: 6px 9px;
  border: 1px solid #355078; border-radius: 8px; background: #14233d; color: #d0e2ff; font-size: 12px; }
.replay-members summary { cursor: pointer; }
.replay-members-list { max-height: 35vh; overflow: auto; padding: 7px 3px; overflow-wrap: anywhere; }
.replay-members-list ol { margin: 4px 0 8px; padding-left: 20px; }
.l2g-edges path { fill: none; stroke: #6888b5; stroke-opacity: .22; stroke-width: 1.05; transition: stroke-opacity .18s; }
.l2g-edges path.is-related { stroke: #9dc9e7; stroke-opacity: .65; stroke-width: 1.5; }
.l2g-edges path.is-muted { stroke-opacity: .055; }
.l2g-node { cursor: pointer; transition: filter .18s; }
.l2g-node.is-muted { filter: saturate(.6) brightness(.72); }
.l2g-node .hit { fill: transparent; pointer-events: all; }
/* 颜色按路径（--h）走，M 和 t 靠**形状**分开：
 *   M = 大圆盘、暗色填充 + 亮色描边（有色星体）；t = 小圆点、实心亮色（行星）。 */
.l2g-node .halo { fill: hsl(var(--h) 80% 60%); opacity: .075; pointer-events: none; }
.l2g-node .core { fill: #16233e; stroke: hsl(var(--h) 76% 64%); stroke-width: 1.6; }
.l2g-node.kind-node .core { fill: hsl(var(--h) 32% 18%); stroke: hsl(var(--h) 62% 67%); stroke-width: 1.6; }
.l2g-node.kind-tx .core { fill: hsl(var(--h) 65% 70%); stroke: hsl(var(--h) 74% 85%); stroke-width: 1; }
.l2g-node.kind-tx .halo { opacity: .08; }
.l2g-node.role-mirror .core {
  fill: #0b1226; stroke: hsl(var(--h) 80% 70%);
  stroke-width: 1.2; stroke-dasharray: 2 2;
}
.l2g-node:hover .core { stroke: #fff; stroke-width: 2.4; }
.l2g-node.kind-group .core { fill: #2d2456; stroke: #ad90f7; }
.l2g-node.kind-group .halo { fill: #ab90f7; opacity: .16; }
.l2g-node.root .core { fill: #101d3d; stroke: #8bc8ff; stroke-width: 3; }
.l2g-node.root .halo { fill: #767de2; opacity: .35; }
.l2g-node.selected .core { stroke: #fff; stroke-width: 3; filter: drop-shadow(0 0 8px #9fd8ff); }
.l2g-node.selected .halo { opacity: .19; }
.l2g-node.dragging { opacity: .4; }
.l2g-node.drop-target .core { fill: #317e7e; stroke: #ffe1ac; stroke-width: 3.5; }
.l2g-label {
  fill: #e2ebf8; font-size: 11.5px; font-weight: 620; pointer-events: none;
  paint-order: stroke; stroke: #0b1226; stroke-width: 3px; stroke-linejoin: round;
}
.l2g-value { fill: #93a8c4; font-size: 10px; pointer-events: none; paint-order: stroke; stroke: #0b1226; stroke-width: 4px; stroke-linejoin: round; }
.l2g-tx { fill: #b2c2d9; font-size: 10.5px; paint-order: stroke; stroke: #0b1226; stroke-width: 4px; stroke-linejoin: round; pointer-events: none; }
.l2g-tx-amt {
  fill: hsl(var(--h) 50% 80%); font-size: 10.5px; font-weight: 550;
  font-variant-numeric: tabular-nums; pointer-events: none;
  paint-order: stroke; stroke: #0b1226; stroke-width: 4px; stroke-linejoin: round;
}
.l2g-more { cursor: pointer; }
.l2g-more circle { fill: #33456b; stroke: #9fb4dd; stroke-width: 1; }
.l2g-more:hover circle { fill: #4a679e; stroke: #dbe7fb; }
.l2g-more.is-open circle { fill: #2d2456; stroke: #ad90f7; }
.l2g-more text { fill: #e8f1ff; font-size: 10px; }
.l2g-root { fill: #f1f6ff; font-size: 14px; font-weight: 700; pointer-events: none; }
.l2g-root-amt {
  fill: #f4d9a8; font-size: 11px; font-weight: 600;
  font-variant-numeric: tabular-nums; pointer-events: none;
  paint-order: stroke; stroke: #0b1226; stroke-width: 4px; stroke-linejoin: round;
}
.l2g-zoom {
  position: absolute; right: 16px; top: 16px; display: flex; align-items: center; gap: 4px;
  background: rgba(17, 29, 49, .92); border: 1px solid #2b3b56; padding: 5px 7px; border-radius: 12px; box-shadow: 0 5px 20px #0002;
  color: #c9d6ef; font-size: 12px;
}
.l2g-zoom button {
  border: 1px solid #3d4d70; background: #16233e; color: #dbe7fb;
  border-radius: 6px; padding: 1px 7px; font-size: 13px; cursor: pointer;
}
.l2g-zoom button:hover { background: #22345a; }
.l2g-help {
  position: absolute; left: 16px; bottom: 12px; right: 16px; margin: 0;
  color: #8195b0; font-size: 11px; line-height: 1.6; pointer-events: none;
}
@media (prefers-reduced-motion: reduce) {
  .l2g-node, .l2g-edges path { transition: none; }
}
.l2g-band {
  position: fixed; border: 1px solid #7fc3e0; background: rgba(127, 195, 224, .15);
  pointer-events: none; z-index: 60;
}
</style>
