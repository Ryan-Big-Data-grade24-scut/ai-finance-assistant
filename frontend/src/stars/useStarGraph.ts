import { computed, type Ref } from 'vue'
import type { LedgerTree, LedgerTx } from '@/api/ledger'

export type ViewMode = 'both' | 'time' | 'category'
type GKind = 'root' | 'lens' | 'group' | 'period' | 'tx' | 'placeholder'
export interface GNode {
  id: string; name: string; kind: GKind; side: 'root' | 'time' | 'category'
  e: number; i: number; count: number; op?: 'ADD' | 'NET'; tx?: LedgerTx
  nodeId?: string; children: GNode[]
  preview?: boolean; sourceId?: string
  peekMore?: boolean; peekParentId?: string
}
export interface Point { node: GNode; x: number; y: number; depth: number; side: 'root' | 'time' | 'category'; hasChildren: boolean; satellite?: boolean; spawnFrom?: {x:number;y:number} }
export interface Edge { from: Point; to: Point }

export interface CameraPan { x: number; y: number }
export interface StarGraphOptions {
  tree: Ref<LedgerTree | null>
  mode: Ref<ViewMode>
  focusStack: Ref<string[]>
  focusPage: Ref<number>
  semanticAnchorId: Ref<string>
  revealLevel: Ref<number>
}

// Collision envelopes include an approximate label width, not just a circle.
export function nodeRadius(p: Point): number {
  if (p.depth === 0) return 64
  if (p.node.kind === 'lens') return 35
  if (p.node.kind === 'tx') return 8
  return Math.max(15, Math.min(28, 13 + Math.log10(Math.max(1, p.node.e + p.node.i)) * 3.8))
}
export function nodeTitle(node: GNode): string {
  if (node.kind === 'tx') return node.name.length > 11 ? node.name.slice(0, 10) + '…' : node.name
  return node.name.length > 12 ? node.name.slice(0, 11) + '…' : node.name
}
export function edgePath(e: Edge): string {
  const mx = (e.from.x + e.to.x) / 2
  return `M ${e.from.x} ${e.from.y} C ${mx} ${e.from.y}, ${mx} ${e.to.y}, ${e.to.x} ${e.to.y}`
}
function nodeBox(p: Point): {x0:number; x1:number; y0:number; y1:number} {
  const r = nodeRadius(p) + 9
  const length = Array.from(nodeTitle(p.node)).reduce((n, c) => n + (/[^\x00-\x7F]/.test(c) ? 13 : 7), 0)
  const label = p.depth === 0 ? 0 : Math.min(182, Math.max(56, length + 38))
  return {
    x0: p.x - r - (p.side === 'time' ? label : 0),
    x1: p.x + r + (p.side === 'category' ? label : 0),
    y0: p.y - Math.max(r, 22), y1: p.y + Math.max(r, 22),
  }
}
export function collisionArea(a: Point, b: Point, gap=12): number {
  const x=nodeBox(a), y=nodeBox(b)
  return Math.max(0, Math.min(x.x1 + gap, y.x1 + gap) - Math.max(x.x0, y.x0)) *
    Math.max(0, Math.min(x.y1 + gap, y.y1 + gap) - Math.max(x.y0, y.y0))
}
function findRoom(parent: Point, node: GNode, all: Point[], level: number, ordinal: number, total: number): {x:number;y:number} {
  const sign = node.side === 'time' ? -1 : 1
  const spread = Math.min(1.90, 0.34 + total * 0.23)
  const fraction = total <= 1 ? 0 : (ordinal - (total - 1) / 2) / ((total - 1) / 2)
  let best = {x: parent.x + sign * 150, y: parent.y}
  let bestScore = Infinity
  // Stable bounded radial search: prefer a natural orbit, then increase radius or fan angle.
  for (let trial=0; trial<90; trial++) {
    const radialStep = Math.floor(trial / 15)
    const wiggle = (trial % 15 === 0 ? 0 : Math.ceil((trial%15)/2) * (trial % 2 ? 1 : -1) * 0.092)
    const theta = (fraction * spread / 2 + wiggle)
    const radius = 140 + level * 36 + radialStep * 54
    const x = Math.max(-870, Math.min(870, parent.x + sign * Math.cos(theta) * radius))
    const y = Math.max(-470, Math.min(470, parent.y + Math.sin(theta) * radius))
    const p:Point = {node,x,y,depth:parent.depth+1,side:node.side,hasChildren:node.children.length>0,satellite:true,spawnFrom:{x:parent.x,y:parent.y}}
    const score = all.reduce((sum, other) => sum + collisionArea(p,other), 0) +
      Math.max(0,Math.abs(x)-815)*12 + Math.max(0,Math.abs(y)-425)*10 + trial * 1.5
    if(score<bestScore) { bestScore=score; best={x,y} }
    if(score<2) break
  }
  return best
}

export function useStarGraph(options: StarGraphOptions) {
  const {tree,mode,focusStack,focusPage,semanticAnchorId,revealLevel}=options
  const PEEK_VISIBLE_LIMIT = 6
  const groups = computed(() => tree.value?.nodes.filter(n => n.kind !== 'root') ?? [])
  const ledgerAmount = (tx: LedgerTx) => tx.effective_amount ?? tx.amount
function txNode(tx: LedgerTx, side: 'time' | 'category'): GNode {
  const amount = ledgerAmount(tx)
  return { id: `${side}:tx:${tx.id}`, name: tx.counterparty || tx.item || `交易 #${tx.id}`, kind: 'tx', side,
    e: tx.direction === 'expense' ? amount : 0, i: tx.direction === 'income' ? amount : 0,
    count: 1, tx, children: [] }
}
function makeCategory(): GNode | null {
  if (!tree.value) return null
  const nodes = new Map<string, GNode>()
  for (const n of tree.value.nodes) nodes.set(n.id, {
    id: `cat:${n.id}`, nodeId: n.id, name: n.name, kind: 'group', side: 'category',
    e: n.e, i: n.i, count: n.count, op: n.op, children: [],
  })
  for (const n of tree.value.nodes) {
    if (n.parent_id) nodes.get(n.parent_id)?.children.push(nodes.get(n.id)!)
  }
  const directly = new Map<string, LedgerTx[]>()
  for (const tx of tree.value.transactions) {
    const list = directly.get(tx.node_id) ?? []
    list.push(tx)
    directly.set(tx.node_id, list)
  }
  for (const [id, node] of nodes) {
    let direct = directly.get(id) ?? []
    direct = [...direct].sort((a, b) => ledgerAmount(b) - ledgerAmount(a) || a.id - b.id)
    // 未分类的根节点成员统一打包成「待分类」，而不是一次画出大量点。
    if (id === tree.value.root_id && direct.length) {
      const e = direct.filter(t => t.direction === 'expense').reduce((s, t) => s + ledgerAmount(t), 0)
      const i = direct.filter(t => t.direction === 'income').reduce((s, t) => s + ledgerAmount(t), 0)
      node.children.push({ id: 'cat:unassigned', name: '待分类', kind: 'placeholder', side: 'category',
        nodeId: tree.value.root_id, e, i, count: direct.length, children: direct.map(t => txNode(t, 'category')) })
    } else node.children.push(...direct.map(t => txNode(t, 'category')))
    node.children.sort((a, b) => b.e + b.i - a.e - a.i || a.name.localeCompare(b.name))
  }
  const root = nodes.get(tree.value.root_id)!
  return { id: 'cat:lens', name: '消费分类', kind: 'lens', side: 'category',
    e: tree.value.root_e, i: tree.value.root_i, count: tree.value.count, children: root.children, nodeId: root.nodeId }
}
function makeTime(): GNode | null {
  if (!tree.value) return null
  const years = new Map<string, GNode>()
  function child(parent: GNode, id: string, name: string): GNode {
    let item = parent.children.find(n => n.id === id)
    if (!item) {
      item = { id, name, kind: 'period', side: 'time', e: 0, i: 0, count: 0, children: [] }
      parent.children.push(item)
    }
    return item
  }
  for (const tx of tree.value.transactions) {
    const year = tx.date.slice(0, 4), month = tx.date.slice(0, 7)
    let y = years.get(year)
    if (!y) {
      y = { id: `time:${year}`, name: `${year} 年`, kind: 'period', side: 'time',
        e: 0, i: 0, count: 0, children: [] }
      years.set(year, y)
    }
    const m = child(y, `time:${month}`, `${Number(month.slice(5))} 月`)
    const d = child(m, `time:${tx.date}`, `${Number(tx.date.slice(8))} 日`)
    d.children.push(txNode(tx, 'time'))
    for (const n of [y, m, d]) {
      n.e += tx.direction === 'expense' ? ledgerAmount(tx) : 0
      n.i += tx.direction === 'income' ? ledgerAmount(tx) : 0
      n.count += 1
    }
  }
  // 时间分支是原始流水的一个投影，不与分类分支的金额再相加。
  return { id: 'time:lens', name: '时间轨迹', kind: 'lens', side: 'time',
    e: [...years.values()].reduce((sum, x) => sum + x.e, 0),
    i: [...years.values()].reduce((sum, x) => sum + x.i, 0),
    count: tree.value.count, children: [...years.values()].sort((a, b) => b.name.localeCompare(a.name)) }
}


// Build the opposite-side galaxy from exactly the transactions inside the focused node.
// This is a visual projection only; it never writes to the authoritative ledger.
function makeTimeFromTransactions(txs: LedgerTx[], lensName: string): GNode {
  const years = new Map<string, GNode>()
  function child(parent: GNode, id: string, name: string): GNode {
    let item = parent.children.find(n => n.id === id)
    if (!item) {
      item = { id, name, kind: 'period', side: 'time', e: 0, i: 0, count: 0, children: [] }
      parent.children.push(item)
    }
    return item
  }
  for (const tx of txs) {
    const year = tx.date.slice(0, 4), month = tx.date.slice(0, 7)
    let y = years.get(year)
    if (!y) {
      y = { id: `time:${year}`, name: `${year} 年`, kind: 'period', side: 'time', e: 0, i: 0, count: 0, children: [] }
      years.set(year, y)
    }
    const m = child(y, `time:${month}`, `${Number(month.slice(5))} 月`)
    const d = child(m, `time:${tx.date}`, `${Number(tx.date.slice(8))} 日`)
    d.children.push(txNode(tx, 'time'))
    for (const n of [y, m, d]) {
      n.e += tx.direction === 'expense' ? ledgerAmount(tx) : 0
      n.i += tx.direction === 'income' ? ledgerAmount(tx) : 0
      n.count += 1
    }
  }
  return {
    id: 'time:lens:subset', name: lensName, kind: 'lens', side: 'time',
    e: txs.reduce((sum, tx) => sum + (tx.direction === 'expense' ? ledgerAmount(tx) : 0), 0),
    i: txs.reduce((sum, tx) => sum + (tx.direction === 'income' ? ledgerAmount(tx) : 0), 0),
    count: txs.length,
    children: [...years.values()].sort((a, b) => b.name.localeCompare(a.name)),
  }
}

function makeCategoryFromTransactions(txs: LedgerTx[], lensName: string): GNode | null {
  if (!tree.value) return null
  const rootId = tree.value.root_id
  const sourceNodes = new Map(tree.value.nodes.map(n => [n.id, n]))
  const root: GNode = {
    id: 'cat:lens:subset', name: lensName, kind: 'lens', side: 'category',
    e: 0, i: 0, count: txs.length, children: [], nodeId: rootId,
  }
  const groupsById = new Map<string, GNode>()
  let unassigned: GNode | null = null

  const amountFor = (tx: LedgerTx) => ({
    e: tx.direction === 'expense' ? ledgerAmount(tx) : 0,
    i: tx.direction === 'income' ? ledgerAmount(tx) : 0,
  })

  for (const tx of txs) {
    const amount = amountFor(tx)
    root.e += amount.e
    root.i += amount.i
    const chain: string[] = []
    let cursor = sourceNodes.get(tx.node_id)
    while (cursor && cursor.id !== rootId) {
      chain.unshift(cursor.id)
      cursor = cursor.parent_id ? sourceNodes.get(cursor.parent_id) : undefined
    }

    if (!chain.length) {
      if (!unassigned) {
        unassigned = {
          id: 'cat:subset:unassigned', name: '待分类', kind: 'placeholder', side: 'category',
          e: 0, i: 0, count: 0, children: [], nodeId: rootId,
        }
        root.children.push(unassigned)
      }
      unassigned.e += amount.e
      unassigned.i += amount.i
      unassigned.count += 1
      unassigned.children.push(txNode(tx, 'category'))
      continue
    }

    let parent = root
    for (const ledgerId of chain) {
      let group = groupsById.get(ledgerId)
      if (!group) {
        const meta = sourceNodes.get(ledgerId)
        group = {
          id: `cat:${ledgerId}`, nodeId: ledgerId, name: meta?.name ?? '未命名分类',
          kind: 'group', side: 'category', e: 0, i: 0, count: 0,
          op: meta?.op ?? 'ADD', children: [],
        }
        groupsById.set(ledgerId, group)
        parent.children.push(group)
      }
      group.e += amount.e
      group.i += amount.i
      group.count += 1
      parent = group
    }
    parent.children.push(txNode(tx, 'category'))
  }

  // Recompute the preview bottom-up using the same ADD/NET semantics as the ledger engine.
  // A NET node keeps only the positive side of its subtree's net amount.
  function aggregate(node: GNode): { e: number; i: number; count: number } {
    if (node.kind === 'tx') return { e: node.e, i: node.i, count: 1 }
    let e = 0, i = 0, count = 0
    for (const child of node.children) {
      const value = aggregate(child)
      e += value.e; i += value.i; count += value.count
    }
    if (node.op === 'NET') {
      if (i >= e) { i = i - e; e = 0 }
      else { e = e - i; i = 0 }
    }
    node.e = e; node.i = i; node.count = count
    return { e, i, count }
  }
  function sortDeep(node: GNode) {
    node.children.sort((a, b) => (b.e + b.i) - (a.e + a.i) || a.name.localeCompare(b.name))
    node.children.forEach(sortDeep)
  }
  aggregate(root)
  sortDeep(root)
  return root
}
const fullGraphs = computed(() => {
  const parts: GNode[] = []
  if (mode.value !== 'category') { const t = makeTime(); if (t) parts.push(t) }
  if (mode.value !== 'time') { const c = makeCategory(); if (c) parts.push(c) }
  return parts
})
// Construct one complete logical hierarchy; the drawing below only renders the current subtree.
const visualRoot = computed<GNode>(() => ({
  id: 'root', name: '全部账目', kind: 'root', side: 'root',
  e: tree.value?.root_e ?? 0, i: tree.value?.root_i ?? 0,
  count: tree.value?.count ?? 0, children: fullGraphs.value,
}))
const hierarchy = computed(() => {
  const nodes = new Map<string, GNode>(), parents = new Map<string, string>()
  function visit(n: GNode, parent?: string) {
    nodes.set(n.id, n)
    if (parent) parents.set(n.id, parent)
    n.children.forEach(child => visit(child, n.id))
  }
  visit(visualRoot.value)
  return { nodes, parents }
})
const focusId = computed(() => focusStack.value[focusStack.value.length - 1] ?? 'root')
const focusNode = computed(() => hierarchy.value.nodes.get(focusId.value) ?? visualRoot.value)
const breadcrumbs = computed(() => focusStack.value.map(id => ({
  id, label: hierarchy.value.nodes.get(id)?.name ?? (id === 'root' ? '全部账目' : '已移除节点'),
})))

function collectTransactions(node: GNode): LedgerTx[] {
  if (node.kind === 'tx' && node.tx) return [node.tx]
  return node.children.flatMap(child => collectTransactions(child))
}
const focusTransactions = computed<LedgerTx[]>(() => {
  if (!tree.value) return []
  // The root contains both a time and category projection, so use the source rows once.
  if (focusNode.value.id === 'root') return tree.value.transactions
  return collectTransactions(focusNode.value)
})
const companionLens = computed<GNode | null>(() => {
  if (!tree.value || mode.value !== 'both' || focusNode.value.id === 'root' || focusNode.value.kind === 'tx') return null
  const txs = focusTransactions.value
  if (!txs.length) return null
  if (focusNode.value.side === 'time') return makeCategoryFromTransactions(txs, `${focusNode.value.name} · 分类`)
  if (focusNode.value.side === 'category') return makeTimeFromTransactions(txs, `${focusNode.value.name} · 时间`)
  return null
})
function clonePreviewTree(node: GNode, namespace: string): GNode {
  return {
    ...node,
    id: `preview:${namespace}:${node.id}`,
    sourceId: node.id,
    preview: true,
    children: node.children.map(child => clonePreviewTree(child, namespace)),
  }
}
const pageSize = 16
const focusTotalPages = computed(() => Math.max(1, Math.ceil(focusNode.value.children.length / pageSize)))
const pageChildren = computed(() => focusNode.value.children.slice(focusPage.value * pageSize, (focusPage.value + 1) * pageSize))

  // The fixed skeleton shows both projections. Semantic zoom grows descendants
  // beside their real parent in local orbits; it never changes the centered node.
  const graph = computed(() => {
    const nodes: Point[] = [], edges: Edge[] = []
    if (!tree.value) return { nodes, edges }
    const head = focusNode.value
    const root: Point = {node:head,x:0,y:0,depth:0,side:head.side,hasChildren:head.children.length>0}
    nodes.push(root)
    const put=(parent:Point,node:GNode,x:number,y:number,depth:number,satellite=false):Point => {
      const p:Point={node,x,y,depth,side:node.side,hasChildren:node.children.length>0,satellite,spawnFrom:satellite?{x:parent.x,y:parent.y}:undefined}
      nodes.push(p); edges.push({from:parent,to:p}); return p
    }
    if(head.id==='root') {
      for(const lens of head.children) {
        const sign=lens.side==='time'?-1:1
        const center=put(root,lens,sign*245,0,1)
        const first=lens.children.slice(0,7)
        first.forEach((child,i)=>put(center,child,sign*555,(i-(first.length-1)/2)*112,2))
      }
    } else {
      const children=pageChildren.value, columns=children.length>9?2:1
      const sign=head.side==='time'?-1:1
      children.forEach((child,index)=>{
        const col=index%columns,row=Math.floor(index/columns)
        const length=Math.ceil((children.length-col)/columns)
        const sep=length>=8?102:Math.min(170,730/Math.max(1,length-1))
        put(root,child,sign*(columns===1?430:col===0?345:650),
          (row-(length-1)/2)*sep+(columns===2&&col===1?18:0),1)
      })
      if(mode.value==='both'&&companionLens.value&&head.kind!=='tx') {
        const lens=clonePreviewTree(companionLens.value,head.side==='time'?'category':'time')
        const sign=head.side==='time'?1:-1
        const lensPoint=put(root,lens,sign*230,0,1)
        const first=lens.children.slice(0,6)
        first.forEach((child,index)=>{
          const y=(index-(first.length-1)/2)*136
          const p=put(lensPoint,child,sign*505,y,2)
          let nested=child.children.filter(item=>item.kind!=='tx')
          const direct=child.children.filter(item=>item.kind==='tx')
          if(direct.length) nested=[{
            id:`${child.id}:direct`,name:'直接交易',kind:'placeholder',side:child.side,
            e:direct.reduce((sum,x)=>sum+x.e,0),i:direct.reduce((sum,x)=>sum+x.i,0),
            count:direct.length,children:direct,preview:true,
          },...nested]
          nested.slice(0,3).forEach((grand,i,visible)=>put(p,grand,sign*775,y+(i-(visible.length-1)/2)*48,3))
        })
      }
    }
    const anchor=semanticAnchorId.value ? nodes.find(p=>p.node.id===semanticAnchorId.value) : undefined
    const levels=revealLevel.value
    if(anchor && anchor.hasChildren && levels>0) {
      const visible=new Map(nodes.map(p=>[p.node.id,p]))
      let frontier=[anchor]
      const limits=[PEEK_VISIBLE_LIMIT,3,2]
      // Hard cap protects both drawing performance and readability on 1000+ transactions.
      for(let level=1;level<=Math.min(3,levels)&&nodes.length<84;level++) {
        const next:Point[]=[]
        for(const parent of frontier) {
          if(nodes.length>=84)break
          const unseen=parent.node.children.filter(c=>!visible.has(c.id))
          const candidates=unseen.slice(0,limits[level-1])
          for(let i=0;i<candidates.length&&nodes.length<84;i++) {
            const child=candidates[i]
            const xy=findRoom(parent,child,nodes,level,i,candidates.length)
            const pt=put(parent,child,xy.x,xy.y,parent.depth+1,true)
            visible.set(child.id,pt);next.push(pt)
          }
          // Expand already-shown children too (important for an anchor with visible first-level nodes).
          for(const child of parent.node.children.slice(0,limits[level-1])) {
            const existing=visible.get(child.id)
            if(existing && !next.includes(existing)) next.push(existing)
          }
          const more=unseen.length-candidates.length
          if(more>0&&nodes.length<84) {
            const placeholder:GNode={id:`peek:more:${parent.node.id}:${level}`,name:`还有 ${more} 项`,
              kind:'placeholder',side:parent.node.side,e:0,i:0,count:more,children:[],
              peekMore:true,peekParentId:parent.node.id,preview:parent.node.preview}
            const xy=findRoom(parent,placeholder,nodes,level,candidates.length,candidates.length+1)
            put(parent,placeholder,xy.x,xy.y,parent.depth+1,true)
          }
        }
        frontier=next.filter(p=>p.hasChildren)
        if(!frontier.length)break
      }
    }
    return {nodes,edges}
  })
  const graphById=computed(()=>new Map(graph.value.nodes.map(p=>[p.node.id,p])))
  return { groups, fullGraphs, visualRoot, hierarchy, focusId, focusNode, breadcrumbs,
    focusTransactions, companionLens, focusTotalPages, pageChildren, graph, graphById }
}
