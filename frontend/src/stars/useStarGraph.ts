import { computed, type Ref } from 'vue'
import type { LedgerTree, LedgerTx } from '@/api/ledger'

// 星图是一张不变的世界地图：筛选决定数据，点击只决定已展开节点和镜头。
export type ViewMode = 'category'
export type GKind = 'root' | 'group' | 'tx' | 'placeholder'
export interface GNode {
  id: string; name: string; kind: GKind; side: 'root' | 'category'
  e: number; i: number; count: number; op?: 'ADD' | 'NET'; tx?: LedgerTx
  nodeId?: string; ledgerKind?: string; children: GNode[]
  peekMore?: boolean; peekParentId?: string
}
export interface Point {
  node: GNode; x: number; y: number; depth: number; side: 'root' | 'category'
  hasChildren: boolean; satellite?: boolean; spawnFrom?: {x:number;y:number}; worldX?:number; worldY?:number
  peripheral?: boolean; focusBranch?: boolean; displayOpacity?: number; displayScale?: number
}
export interface Edge { from: Point; to: Point }
export interface CameraPan { x: number; y: number }
export interface StarGraphOptions {
  tree: Ref<LedgerTree | null>
  periodLabel: Ref<string>
  categoryFilter: Ref<string>
  expandedIds: Ref<string[]>
  visibleLimits: Ref<Record<string, number>>
}

export function nodeRadius(p: Point): number {
  if (p.node.kind === 'root') return 68
  if (p.node.kind === 'tx') return 9
  if (p.node.peekMore) return 16
  return Math.max(19, Math.min(33, 16 + Math.log10(Math.max(1, p.node.e + p.node.i)) * 4))
}
export function nodeTitle(n: GNode): string {
  const cap = n.kind === 'root' ? 12 : n.kind === 'tx' ? 12 : 15
  return n.name.length > cap ? n.name.slice(0, cap - 1) + '…' : n.name
}
export function edgePath(e: Edge): string {
  // 曲线把所有分类连接到同一个时间区间，而非强制分成左右两个星系。
  const x=(e.from.x+e.to.x)/2, y=(e.from.y+e.to.y)/2
  return `M ${e.from.x} ${e.from.y} Q ${x} ${y} ${e.to.x} ${e.to.y}`
}
function value(tx: LedgerTx) { return tx.effective_amount ?? tx.amount }
function txNode(tx: LedgerTx): GNode {
  const amount = value(tx)
  return { id:`category:tx:${tx.id}`, name:tx.counterparty || tx.item || `交易 #${tx.id}`,
    kind:'tx', side:'category', e:tx.direction==='expense'?amount:0,
    i:tx.direction==='income'?amount:0, count:1, tx, children:[] }
}
export function useStarGraph({tree,periodLabel,categoryFilter,expandedIds,visibleLimits}: StarGraphOptions) {
  const groups = computed(() => tree.value?.nodes.filter(n => n.kind !== 'root' && (n.count > 0 || n.e > 0 || n.i > 0)) ?? [])
  const visualRoot = computed<GNode>(() => {
    const current = tree.value
    const root:GNode = {id:'root',name:periodLabel.value,kind:'root',side:'root',
      e:current?.root_e??0,i:current?.root_i??0,count:current?.count??0,children:[],nodeId:current?.root_id}
    if (!current) return root
    const nodes = new Map(current.nodes.map(n => [n.id, {
      id:`cat:${n.id}`,name:n.name,kind:'group' as const,side:'category' as const,
      nodeId:n.id,ledgerKind:n.kind,op:n.op,e:n.e,i:n.i,count:n.count,children:[] as GNode[],
    }]))
    for (const n of current.nodes) if (n.parent_id) nodes.get(n.parent_id)?.children.push(nodes.get(n.id)!)
    const nonEmpty=(node:GNode)=>node.kind!=='group' || node.count>0 || node.e>0 || node.i>0 || node.children.some(nonEmpty)
    const direct = new Map<string, LedgerTx[]>()
    for (const tx of current.transactions) {
      const list=direct.get(tx.node_id)??[];list.push(tx);direct.set(tx.node_id,list)
    }
    for (const [id,n] of nodes) {
      const txs=(direct.get(id)??[]).sort((a,b)=>value(b)-value(a)||a.id-b.id)
      if (id===current.root_id && txs.length) {
        n.children.push({id:'cat:unassigned',name:'待分类',kind:'placeholder',side:'category',
          nodeId:id,e:txs.filter(t=>t.direction==='expense').reduce((s,t)=>s+value(t),0),
          i:txs.filter(t=>t.direction==='income').reduce((s,t)=>s+value(t),0),
          count:txs.length,children:txs.map(txNode)})
      } else n.children.push(...txs.map(txNode))
      n.children=n.children.filter(nonEmpty)
      n.children.sort((a,b)=>(b.e+b.i)-(a.e+a.i)||a.name.localeCompare(b.name))
    }
    const parent=nodes.get(current.root_id)
    if (categoryFilter.value && nodes.has(categoryFilter.value) && categoryFilter.value!==current.root_id) {
      root.children=[nodes.get(categoryFilter.value)!]
    } else root.children=parent?.children??[]
    return root
  })
  const hierarchy = computed(() => {
    const nodes=new Map<string,GNode>(),parents=new Map<string,string>()
    const visit=(node:GNode,parent?:string) => {
      nodes.set(node.id,node); if(parent)parents.set(node.id,parent)
      node.children.forEach(child=>visit(child,node.id))
    }
    visit(visualRoot.value)
    return {nodes,parents}
  })
  const graph = computed<{nodes:Point[];edges:Edge[]}>(() => {
    const nodes:Point[]=[], edges:Edge[]=[]
    if(!tree.value)return {nodes,edges}
    const head:Point={node:visualRoot.value,x:0,y:0,depth:0,side:'root',hasChildren:visualRoot.value.children.length>0}
    nodes.push(head)
    const drawn=new Map<string,Point>([['root',head]])
    const add=(parent:Point,node:GNode,x:number,y:number):Point=>{
      const p:Point={node,x,y,depth:parent.depth+1,side:node.side,hasChildren:node.children.length>0,
        satellite:parent.depth>0,spawnFrom:{x:parent.x,y:parent.y}}
      nodes.push(p);edges.push({from:parent,to:p});drawn.set(node.id,p);return p
    }
    const initial=visualRoot.value.children
    const count=initial.length
    // 各分类按角度平均分布于中心时间节点周围，不再统一放在右侧。
    const radius=Math.max(380,Math.min(540,330+count*16))
    initial.forEach((node,index)=>{
      const angle=-Math.PI/2+2*Math.PI*index/Math.max(1,count)
      add(head,node,Math.cos(angle)*radius,Math.sin(angle)*radius*.74)
    })
    // 已展开分支逐步显示，原有点的世界坐标不变，避免点击造成整张图跳页。
    const queue=[...nodes.slice(1)]
    const open=new Set(expandedIds.value)
    const MAX_DRAWN=200
    while(queue.length && nodes.length<MAX_DRAWN){
      const parent=queue.shift()!
      if(!open.has(parent.node.id)||!parent.node.children.length)continue
      const children=parent.node.children
      const limit=Math.max(16,visibleLimits.value[parent.node.id]??16)
      const shown=Math.min(children.length,limit,MAX_DRAWN-nodes.length-1)
      const outward=Math.atan2(parent.y,parent.x)||(-Math.PI/2)
      for(let i=0;i<shown;i++){
        const child=children[i]
        if(drawn.has(child.id))continue
        // 16 个一圈，新增批次不改变此前已展示节点的位置。
        const ring=Math.floor(i/16),slot=i%16
        const slots=Math.min(16,children.length-ring*16)
        const spread=slots<=1?0:(parent.depth<=1?Math.PI*1.18:Math.PI*1.32)
        const angle=outward-spread/2+(slots<=1?0:(slot/(slots-1))*spread)
        const distance=230+ring*138+Math.min(parent.depth,3)*42
        const p=add(parent,child,parent.x+Math.cos(angle)*distance,parent.y+Math.sin(angle)*distance)
        queue.push(p)
      }
      if(shown<children.length&&nodes.length<MAX_DRAWN){
        const more=children.length-shown
        const sentinel:GNode={id:`more:${parent.node.id}`,name:nodes.length>=MAX_DRAWN-1?'请缩小筛选范围':`再展开 ${Math.min(16,more)} 项`,
          kind:'placeholder',side:'category',e:0,i:0,count:more,children:[],peekMore:true,peekParentId:parent.node.id}
        const a=outward+Math.PI*.77
        add(parent,sentinel,parent.x+Math.cos(a)*245,parent.y+Math.sin(a)*245)
      }
    }
    return {nodes,edges}
  })
  const graphById=computed(()=>new Map(graph.value.nodes.map(p=>[p.node.id,p])))
  return {groups,visualRoot,hierarchy,graph,graphById}
}
