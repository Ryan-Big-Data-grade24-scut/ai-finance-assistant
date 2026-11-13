<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { applyLedgerOp, getLiveLedgerTree, historyLedger } from '@/api/ledger'
import type { LedgerOperation, LedgerTree, LedgerTx, StarFilters } from '@/api/ledger'
import { useStarGraph, type Point } from '@/stars/useStarGraph'
import { useStarInteraction } from '@/stars/useStarInteraction'
import StarCanvas from '@/components/StarCanvas.vue'
import StarInspector from '@/components/StarInspector.vue'

const tree=ref<LedgerTree|null>(null)
const allTransactions=ref<LedgerTx[]>([])
const periodMode=ref<'all'|'year'|'month'|'day'|'custom'>('all')
const periodValue=ref(''),startDate=ref(''),endDate=ref(''),categoryFilter=ref('')
const refreshing=ref(false),loading=ref(true),busy=ref(false)
const error=ref(''),notice=ref('')
let queryVersion=0
const selectedId=ref('root'),cameraFocusId=ref('root'),hoveredId=ref('')
const expandedIds=ref<string[]>([])
const visibleLimits=ref<Record<string,number>>({})
const zoom=ref(.84),pan=ref({x:0,y:0})
const canvasRef=ref<InstanceType<typeof StarCanvas>|null>(null)
const inspectorOpen=ref(false)
const newGroupOpen=ref(false),groupName=ref(''),groupParent=ref(''),groupOp=ref<'ADD'|'NET'>('ADD')
const targetNodeId=ref('')
const search=ref(''),searchOpen=ref(false)
const fmt=(n:number)=>new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}).format(n)
const reportPeriod=computed(()=>{
  const f=activeFilters.value
  return !f.date_from&&!f.date_to?'全部已导入交易':`${f.date_from||'最早'} — ${f.date_to||'最新'}`
})
const periodRootLabel=computed(()=>{
  if(periodMode.value==='year')return `${periodValue.value||'所选'}年`
  if(periodMode.value==='month')return periodValue.value?`${periodValue.value.replace('-','年')}月`:'所选月份'
  if(periodMode.value==='day')return periodValue.value||'所选日期'
  if(periodMode.value==='custom')return '自定义区间'
  return '全部时间'
})
const availableYears=computed(()=>[...new Set(allTransactions.value.map(t=>t.date.slice(0,4)))].sort().reverse())
const availableMonths=computed(()=>[...new Set(allTransactions.value.map(t=>t.date.slice(0,7)))].sort().reverse())
const availableDays=computed(()=>[...new Set(allTransactions.value.map(t=>t.date))].sort().reverse())
const periodOptions=computed(()=>periodMode.value==='year'?availableYears.value:periodMode.value==='month'?availableMonths.value:availableDays.value)
const activeFilters=computed<StarFilters>(()=>{
  let date_from='',date_to=''
  if(periodMode.value==='year'&&periodValue.value){date_from=`${periodValue.value}-01-01`;date_to=`${periodValue.value}-12-31`}
  else if(periodMode.value==='month'&&periodValue.value){
    const [y,m]=periodValue.value.split('-').map(Number)
    date_from=`${periodValue.value}-01`
    date_to=`${periodValue.value}-${String(new Date(y,m,0).getDate()).padStart(2,'0')}`
  }else if(periodMode.value==='day'&&periodValue.value){date_from=periodValue.value;date_to=periodValue.value}
  else if(periodMode.value==='custom'){date_from=startDate.value;date_to=endDate.value}
  return {date_from:date_from||undefined,date_to:date_to||undefined,category_node_id:categoryFilter.value||undefined}
})
const selectedCountHint=computed(()=>`显示 ${tree.value?.count??0} / ${tree.value?.total_count??0} 笔`)
const {groups,hierarchy,graph,graphById}=useStarGraph({tree,periodLabel:periodRootLabel,categoryFilter,expandedIds,visibleLimits})
// Peripheral context: keep the time center and other primary categories visible
// at the canvas rim, even when a specific cluster is magnified.
const renderGraph=computed(()=>{
  if(cameraFocusId.value==='root')return graph.value
  const focus=graphById.value.get(cameraFocusId.value)
  if(!focus)return graph.value
  const isInFocusedBranch=(id:string)=>{
    if(id===focus.node.id)return true
    let cursor=id
    while(hierarchy.value.parents.has(cursor)){
      const parent=hierarchy.value.parents.get(cursor)!
      if(parent===focus.node.id)return true
      cursor=parent
    }
    return false
  }
  const moved=new Map<string,Point>()
  for(const point of graph.value.nodes){
    let x=point.x,y=point.y
    let peripheral=false,focusBranch=false,displayOpacity=1,displayScale=1
    if(point.node.id!==focus.node.id){
      if(isInFocusedBranch(point.node.id)){
        focusBranch=true
      }else{
        peripheral=true
        const baseAngle=Math.atan2(point.y-focus.y,point.x-focus.x)||Math.atan2(point.y,point.x)||-Math.PI/2
        const edgeX=point.depth===0?560:620
        const edgeY=point.depth===0?235:285
        x=focus.x+Math.cos(baseAngle)*edgeX
        y=focus.y+Math.sin(baseAngle)*edgeY
        displayOpacity=point.depth===0?.16:.28
        displayScale=point.depth===0?.68:.82
      }
    }
    moved.set(point.node.id,{...point,x,y,worldX:point.x,worldY:point.y,peripheral,focusBranch,displayOpacity,displayScale})
  }
  return {nodes:[...moved.values()],edges:graph.value.edges.map(edge=>({
    from:moved.get(edge.from.node.id)!,to:moved.get(edge.to.node.id)!,
  }))}
})
const selected=computed(()=>graphById.value.get(selectedId.value)??graphById.value.get('root'))
const nodeSelector=computed(()=>tree.value?.nodes??[])
const matchingTx=computed(()=>{
  const q=search.value.trim().toLowerCase()
  return q&&tree.value?tree.value.transactions.filter(t=>`${t.item} ${t.counterparty} ${t.amount} ${t.effective_amount??t.amount} ${t.date}`.toLowerCase().includes(q)).slice(0,15):[]
})
let cameraFrame=0
function stopCamera(){if(cameraFrame){window.cancelAnimationFrame(cameraFrame);cameraFrame=0}}
function animateCamera(x:number,y:number,scale:number){
  stopCamera()
  const fromPan={...pan.value},fromZoom=zoom.value,start=performance.now(),duration=430
  const step=(now:number)=>{
    const t=Math.min(1,(now-start)/duration),ease=1-Math.pow(1-t,3)
    pan.value={x:fromPan.x+(x-fromPan.x)*ease,y:fromPan.y+(y-fromPan.y)*ease}
    zoom.value=fromZoom+(scale-fromZoom)*ease
    cameraFrame=t<1?window.requestAnimationFrame(step):0
  }
  cameraFrame=window.requestAnimationFrame(step)
}
function focus(){
  stopCamera()
  expandedIds.value=[];visibleLimits.value={}
  selectedId.value='root';cameraFocusId.value='root';inspectorOpen.value=false
  pan.value={x:0,y:0};zoom.value=.84
  interaction.resetLocal()
}
function expandAll(){
  expandedIds.value=hierarchy.value.nodes.get('root')?.children.map(n=>n.id)??[]
  notice.value='已展开首层分类，单击任一星体可进一步居中查看。'
}
function collapseAll(){focus()}
function branchPath(nodeId:string){
  const path:string[]=[]
  let cursor=nodeId
  while(hierarchy.value.parents.has(cursor)){
    const parent=hierarchy.value.parents.get(cursor)!
    if(parent!=='root')path.unshift(parent)
    cursor=parent
  }
  return path
}
// 再次单击当前焦点时只退回直接父节点，而不是重置到时间根节点。
// 交易节点虽然没有子节点，也同样支持“单击进入 / 再单击返回”。
function returnToParent(point:Point){
  const parentId=hierarchy.value.parents.get(point.node.id)??'root'
  const parent=graphById.value.get(parentId)
  inspectorOpen.value=false
  if(parentId==='root'||!parent){
    expandedIds.value=[]
    visibleLimits.value={}
    selectedId.value='root'
    cameraFocusId.value='root'
    animateCamera(0,0,.84)
    return
  }
  selectedId.value=parentId
  cameraFocusId.value=parentId
  // 父节点仍需保持展开，才能看到刚才返回的子分类或交易。
  expandedIds.value=[...branchPath(parentId),parentId]
  const scale=1.16
  animateCamera(-parent.x*scale,-parent.y*scale,scale)
}
function focusPoint(point:Point, allowReturn=true){
  if(point.node.peekMore){
    const parent=point.node.peekParentId
    if(parent)visibleLimits.value={...visibleLimits.value,[parent]:(visibleLimits.value[parent]??16)+16}
    return
  }
  if(point.node.id==='root'){focus();return}
  if(allowReturn && point.node.id===cameraFocusId.value){
    returnToParent(point)
    return
  }
  selectedId.value=point.node.id
  cameraFocusId.value=point.node.id
  inspectorOpen.value=false
  const path=branchPath(point.node.id)
  expandedIds.value=point.hasChildren?[...path,point.node.id]:path
  const scale=point.node.kind==='tx'?Math.max(1.12,zoom.value):1.16
  animateCamera(-(point.worldX??point.x)*scale,-(point.worldY??point.y)*scale,scale)
  if(point.node.tx)targetNodeId.value=point.node.tx.node_id
}
let clickTimer:ReturnType<typeof setTimeout>|null=null
function clearClickTimer(){if(clickTimer!==null){clearTimeout(clickTimer);clickTimer=null}}
function onNodeClick(point:Point){
  if(interaction.shouldIgnoreClick())return
  clearClickTimer()
  clickTimer=setTimeout(()=>{clickTimer=null;focusPoint(point)},270)
}
function onNodeDouble(point:Point){
  clearClickTimer()
  if(interaction.shouldIgnoreClick())return
  if(point.node.peekMore){focusPoint(point);return}
  selectedId.value=point.node.id
  targetNodeId.value=point.node.tx?.node_id||point.node.nodeId||tree.value?.root_id||''
  inspectorOpen.value=true
}
function openParents(nodeId?:string){
  const node=nodeId?hierarchy.value.nodes.get(nodeId):selected.value?.node
  groupParent.value=(node?.kind==='group'&&node.nodeId)?node.nodeId:(node?.kind==='placeholder'&&node.nodeId)?node.nodeId:tree.value?.root_id||''
  groupOp.value='ADD';groupName.value='';newGroupOpen.value=true
}
async function saveGroup(){
  if(!tree.value||!groupName.value.trim())return
  await runOp({op:'create_node',name:groupName.value.trim(),parent_id:groupParent.value,op_value:groupOp.value})
  if(!error.value)newGroupOpen.value=false
}
async function dissolveSelected(){
  const n=selected.value?.node
  if(!tree.value||n?.kind!=='group'||!n.nodeId||n.nodeId===tree.value.root_id||n.ledgerKind==='external_income')return
  if(!window.confirm(`确定解散「${n.name}」分组吗？子分组和交易将回到其上级分类，原始账单不会被删除。此操作可撤销。`))return
  const nodeId=n.nodeId
  await runOp({op:'dissolve_node',node_id:nodeId})
  if(!error.value){focus();if(categoryFilter.value===nodeId)categoryFilter.value=''}
}
function acceptTree(value:LedgerTree){
  tree.value=value
  void nextTick(()=>{if(!hierarchy.value.nodes.has(selectedId.value)){selectedId.value='root';inspectorOpen.value=false}})
}
async function runOp(op:LedgerOperation){
  if(!tree.value||busy.value)return
  busy.value=true;error.value='';notice.value=''
  try{
    acceptTree(await applyLedgerOp(tree.value.book.id,op,tree.value.seq))
    await loadLive(false)
    notice.value='修改已保存 · 账本审计通过'
  }catch(e){error.value=e instanceof Error?e.message:'操作失败'}
  finally{busy.value=false}
}
async function runHistory(action:'undo'|'redo'|'reset'){
  if(!tree.value||busy.value)return
  if(action==='reset'&&!window.confirm('确定回到 M₀ 吗？全部分类节点会被清除，但你可以撤销这一步。'))return
  busy.value=true;error.value='';notice.value=''
  try{
    acceptTree(await historyLedger(tree.value.book.id,action,tree.value.seq))
    await loadLive(false);focus()
    notice.value=action==='undo'?'已撤销上一步':action==='redo'?'已恢复下一步':'已回到 M₀，可撤销'
  }catch(e){error.value=e instanceof Error?e.message:'操作失败'}
  finally{busy.value=false}
}
async function loadLive(initial=false){
  const requestId=++queryVersion
  if(initial)loading.value=true;else refreshing.value=true
  error.value=''
  try{
    if(initial||!allTransactions.value.length){
      const full=await getLiveLedgerTree()
      if(requestId!==queryVersion)return
      allTransactions.value=full.transactions
      if(!activeFilters.value.date_from&&!activeFilters.value.date_to&&!activeFilters.value.category_node_id){acceptTree(full);return}
    }
    const value=await getLiveLedgerTree(activeFilters.value)
    if(requestId!==queryVersion)return
    acceptTree(value)
  }catch(e){if(requestId===queryVersion)error.value=e instanceof Error?e.message:'星图读取失败'}
  finally{if(requestId===queryVersion){loading.value=false;refreshing.value=false}}
}
function reloadLive(){void loadLive(true)}
const selectedYear=computed({
  get:()=>periodValue.value.slice(0,4)||availableYears.value[0]||'',
  set:(year:string)=>{periodValue.value=periodMode.value==='year'?year:periodMode.value==='month'?(availableMonths.value.find(x=>x.startsWith(`${year}-`))??''):(availableDays.value.find(x=>x.startsWith(`${year}-`))??'')}
})
const selectedMonth=computed({
  get:()=>periodValue.value.slice(0,7),
  set:(month:string)=>{periodValue.value=periodMode.value==='day'?(availableDays.value.find(x=>x.startsWith(`${month}-`))??''):month}
})
const selectedDay=computed({get:()=>periodValue.value,set:(day:string)=>{periodValue.value=day}})
const monthsInYear=computed(()=>availableMonths.value.filter(x=>x.startsWith(`${selectedYear.value}-`)))
const daysInMonth=computed(()=>availableDays.value.filter(x=>x.startsWith(`${selectedMonth.value}-`)))
function changePeriodMode(value:typeof periodMode.value){periodMode.value=value;periodValue.value=periodOptions.value[0]||''}
function stepPeriod(older:boolean){
  const idx=periodOptions.value.indexOf(periodValue.value),next=idx+(older?1:-1)
  if(next>=0&&next<periodOptions.value.length)periodValue.value=periodOptions.value[next]
}
function resetFilters(){periodMode.value='all';periodValue.value='';startDate.value='';endDate.value='';categoryFilter.value='';focus()}
function getGroupTrail(nodeId:string){
  if(!tree.value)return '暂无分类'
  const byId=new Map(tree.value.nodes.map(n=>[n.id,n]))
  const names:string[]=[];let node=byId.get(nodeId)
  while(node){if(node.kind!=='root')names.unshift(node.name);node=node.parent_id?byId.get(node.parent_id):undefined}
  return names.join(' / ')||'未分类'
}
const interaction=useStarInteraction({
  zoom,pan,hoveredId,graph:renderGraph,
  getCanvas:()=>({svg:canvasRef.value?.getSvg()??null,scene:canvasRef.value?.getScene()??null}),
  focusPoint,runOp,
})
const {dragLocation,dropId,dropPanel,onMove,onPointerUp}=interaction
function onCanvasDown(event:PointerEvent){stopCamera();interaction.onCanvasDown(event)}
function startNode(event:PointerEvent,point:Point){stopCamera();interaction.startNode(event,point)}
function onWheel(event:WheelEvent){stopCamera();interaction.onWheel(event)}
function zoomBy(step:number){stopCamera();interaction.zoomBy(step)}
async function focusTx(tx:LedgerTx){
  const id=`category:tx:${tx.id}`
  const parents:string[]=[];let cursor=id
  while(hierarchy.value.parents.has(cursor)){
    const parent=hierarchy.value.parents.get(cursor)!
    if(parent!=='root')parents.unshift(parent)
    cursor=parent
  }
  expandedIds.value=[...new Set([...expandedIds.value,...parents])]
  // 搜索不是分页：在同一世界中展开该交易的父分类，再移动镜头。
  for(let i=0;i<parents.length;i++){
    const parent=parents[i],child=parents[i+1]??id
    const node=hierarchy.value.nodes.get(parent)
    const index=node?.children.findIndex(item=>item.id===child)??-1
    if(index>=16)visibleLimits.value={...visibleLimits.value,[parent]:index+1}
  }
  await nextTick()
  const point=graphById.value.get(id)
  if(point){focusPoint(point,false);targetNodeId.value=tx.node_id;inspectorOpen.value=true}
  else notice.value='该交易仍在当前筛选范围内，但可见节点过多，请缩小时间或分类范围。'
  searchOpen.value=false
}
async function moveSelectedTx(){
  if(selected.value?.node.tx&&targetNodeId.value)
    await runOp({op:'reassign',tx_id:selected.value.node.tx.id,to_node_id:targetNodeId.value})
}
async function changeSelectedOp(){
  const n=selected.value?.node
  if(n?.nodeId&&n.kind==='group'&&n.nodeId!==tree.value?.root_id)
    await runOp({op:'set_op',node_id:n.nodeId,op_value:n.op==='NET'?'ADD':'NET'})
}
function onPageFocus(){if(document.visibilityState==='visible')reloadLive()}
watch(activeFilters,()=>{focus();void loadLive()})
onMounted(()=>{void loadLive(true);window.addEventListener('focus',onPageFocus);document.addEventListener('visibilitychange',onPageFocus)})
onBeforeUnmount(()=>{stopCamera();clearClickTimer();window.removeEventListener('focus',onPageFocus);document.removeEventListener('visibilitychange',onPageFocus)})
</script>
<template>
  <div class="nebula-page">
    <div class="nebula-heading">
      <div>
        <div class="eyebrow"><span class="pulse-dot"></span> FINANCE UNIVERSE <span class="eyebrow-index">/ INTERACTIVE LEDGER</span></div>
        <h1>星云<span>账本</span><span class="heading-star">✧</span></h1>
        <p>让每一笔交易，都在你的财务宇宙里找到位置。</p>
      </div>
      <div class="heading-actions">
        <span class="live-indicator"><span class="pulse-dot"></span> 自动同步已导入账单</span>
        <button class="btn-muted" :disabled="loading || refreshing" @click="reloadLive">⟳ {{ refreshing ? '同步中' : '刷新账单' }}</button>
        <button class="btn-primary" :disabled="!tree" @click="openParents()">＋ 添加分组</button>
      </div>
    </div>

    <div v-if="error" class="message message-error">{{ error }} <button @click="error = ''">×</button></div>
    <div v-if="notice" class="message message-success">{{ notice }} <button @click="notice = ''">×</button></div>
    <div class="metrics">
      <div class="metric"><div class="metric-label">总账支出 <span class="m-orbit">◌</span></div><div class="metric-value">¥ {{ fmt(tree?.root_e ?? 0) }}</div><div class="metric-sub">EXPENSE <span class="metric-pill coral">支出</span></div></div>
      <div class="metric"><div class="metric-label">总账收入 <span class="m-orbit">◌</span></div><div class="metric-value">¥ {{ fmt(tree?.root_i ?? 0) }}</div><div class="metric-sub">INCOME <span class="metric-pill cyan">收入</span></div></div>
      <div class="metric"><div class="metric-label">净流入 <span class="m-orbit">◌</span></div><div class="metric-value" :class="(tree?.root_net ?? 0) >= 0 ? 'mint' : 'salmon'">{{ (tree?.root_net ?? 0) >= 0 ? '+' : '−' }} ¥ {{ fmt(Math.abs(tree?.root_net ?? 0)) }}</div><div class="metric-sub">NET CASH FLOW</div></div>
      <div class="metric metric-small"><div class="metric-label">星系规模 <span class="m-orbit">✧</span></div><div class="metric-value">{{ tree?.count ?? 0 }} <span class="unit">笔交易</span></div><div class="metric-sub">全库 {{ tree?.total_count ?? 0 }} 笔 · {{ groups.length }} 个分类节点</div></div>
    </div>

    <section class="star-filters" aria-label="星图范围和层级">
      <div class="quick-date">
        <label>快捷时间定位</label>
        <div class="date-segments" role="group" aria-label="选择时间范围">
          <button v-for="option in ([['all','全部'],['year','年'],['month','月'],['day','日'],['custom','区间']] as const)"
                  :key="option[0]" :class="{active: periodMode === option[0]}" @click="changePeriodMode(option[0])">{{ option[1] }}</button>
        </div>
      </div>
      <div v-if="periodMode === 'year' || periodMode === 'month' || periodMode === 'day'" class="date-cascade">
        <button class="date-arrow" title="上一时间段" :disabled="periodOptions.indexOf(periodValue) >= periodOptions.length - 1" @click="stepPeriod(true)">‹</button>
        <select v-model="selectedYear" aria-label="年份"><option v-for="y in availableYears" :key="y" :value="y">{{ y }}年</option></select>
        <select v-if="periodMode !== 'year'" v-model="selectedMonth" aria-label="月份"><option v-for="m in monthsInYear" :key="m" :value="m">{{ Number(m.slice(5)) }}月</option></select>
        <select v-if="periodMode === 'day'" v-model="selectedDay" aria-label="日期"><option v-for="d in daysInMonth" :key="d" :value="d">{{ Number(d.slice(8)) }}日</option></select>
        <button class="date-arrow" title="下一时间段" :disabled="periodOptions.indexOf(periodValue) <= 0" @click="stepPeriod(false)">›</button>
      </div>
      <template v-if="periodMode === 'custom'"><div class="filter-field"><label for="star-from">开始</label><input id="star-from" v-model="startDate" type="date"/></div><div class="filter-field"><label for="star-to">结束</label><input id="star-to" v-model="endDate" type="date"/></div></template>
      <div class="filter-field filter-category"><label for="star-category-filter">分类范围</label>
        <select id="star-category-filter" v-model="categoryFilter"><option value="">全部分类</option><option v-for="n in groups" :key="n.id" :value="n.id">{{ getGroupTrail(n.id) }}</option></select>
      </div>
      <span class="filter-hint">时间范围通过上方筛选切换；单击聚焦，双击管理节点</span>
      <button class="filter-reset" @click="resetFilters">重置筛选</button>
      <div class="filter-summary">{{ selectedCountHint }}</div>
    </section>

    <section class="galaxy-shell">
      <div class="galaxy-toolbar">
        <div class="star-map-heading">✧ 时间区间 <span>→</span> 分类星图 <small>单击聚焦 / 再单击返回上级 · 双击详情</small></div>
        <div class="galaxy-tools">
          <span class="time-range">{{ reportPeriod }}</span>
          <span class="tool-separator"></span>
          <button title="撤销" :disabled="!tree?.can_undo || busy" @click="runHistory('undo')">↶</button>
          <button title="重做" :disabled="!tree?.can_redo || busy" @click="runHistory('redo')">↷</button>
          <button title="展开首层分类" @click="expandAll">⊞</button>
          <button title="收起局部星图" @click="collapseAll">▣</button>
          <button title="回到时间区间中心" @click="focus">◎</button>
        </div>
      </div>
      <div class="star-status"><span>当前范围：{{ reportPeriod }}</span><span>镜头位置：{{ selected?.node.name || periodRootLabel }}</span><span>切换层级请使用上方时间／分类筛选</span></div>
      <div class="universe">
        <div class="nebula-aurora nebula-aurora-left"></div>
        <div class="nebula-aurora nebula-aurora-right"></div>


        <div class="canvas-hint">✧ 滚轮缩放 · 单击聚焦 / 再单击返回上一级 · 双击打开详情与编辑 · 拖拽交易可调整分类</div>
        <div v-if="!loading && tree && tree.count === 0" class="empty-ledger">
          <h3>{{ tree.total_count ? '当前筛选没有匹配的交易' : '当前数据库还没有可显示的账单' }}</h3>
          <p>{{ tree.total_count ? '试着切换“全部时间 / 全部分类”。' : '请先去“导入账单”上传流水，导入成功后回到这里即可自动显示。' }}</p>
          <button @click="resetFilters">显示全部交易</button>
          <small v-if="!tree.total_count">数据库：{{ tree.database_path }}</small>
        </div>
        <div v-if="loading" class="universe-loading"><span class="loader-ring"></span>正在绘制你的财务宇宙…</div>
        <StarCanvas v-else ref="canvasRef" :graph="renderGraph" :zoom="zoom" :pan="pan"
          :selected-id="selectedId" :hovered-id="hoveredId" :drop-id="dropId" :drag-location="dragLocation"
          @canvas-down="onCanvasDown" @canvas-move="onMove" @canvas-up="onPointerUp" @canvas-wheel="onWheel"
          @node-down="startNode" @node-click="onNodeClick" @node-double="onNodeDouble"
          @node-enter="hoveredId = $event" @node-leave="hoveredId = hoveredId === $event ? '' : hoveredId"/>
        <div v-if="dragLocation" ref="dropPanel" class="drag-targets">
          <strong>拖到目标分类后松开</strong>
          <button v-for="node in nodeSelector" :key="node.id" :data-target="node.id"
                  :class="{highlight: dropId === node.id}"
                  :disabled="node.id === (hierarchy.nodes.get(selectedId)?.tx?.node_id ?? '')">
            {{ node.kind === 'root' ? '未分类（总账）' : node.name }}
          </button>
        </div>
        <div class="zoom-controls">
          <button @click="zoomBy(0.17)" title="放大">＋</button>
          <span>{{ Math.round(zoom * 100) }}%</span>
          <button @click="zoomBy(-0.17)" title="缩小">－</button>
        </div>
        <div class="legend"><span><i class="legend-time"></i> 时间区间中心</span><span><i class="legend-category"></i> 分类</span><span><i class="legend-tx"></i> 交易</span></div>

        <StarInspector v-if="inspectorOpen && selected" :selected="selected" :node-selector="nodeSelector"
          :tree="tree" :target-node-id="targetNodeId" :busy="busy" @close="inspectorOpen = false"
          @update:target-node-id="targetNodeId = $event" @move-tx="moveSelectedTx" @change-op="changeSelectedOp"
          @add-group="openParents(selected?.node.id)" @dissolve-group="dissolveSelected" />
      </div>
      <footer class="galaxy-footer"><div><span class="audit-mark">✓</span> 代数账本 · {{ tree?.invariants_ok ? '结构审计通过' : '待校验' }} <span class="sep">/</span> 修改只写入独立账本层</div><div>Revision {{ tree?.seq ?? '—' }} <span class="sep">·</span> <button @click="runHistory('reset')">回到 M₀</button></div></footer>
    </section>
    <div class="below-galaxy">
      <div class="search-box"><span>⌕</span><input v-model="search" placeholder="查找交易、商户、金额或日期" @focus="searchOpen = true" @keydown.esc="searchOpen = false"/><button v-if="search" @click="search = ''; searchOpen = false">×</button>
        <div v-if="searchOpen && search && matchingTx.length" class="search-results"><button v-for="tx in matchingTx" :key="tx.id" @click="focusTx(tx)"><span>{{ tx.counterparty || tx.item || `交易 #${tx.id}` }}</span><small>{{ tx.date }} · ¥{{ fmt(tx.effective_amount ?? tx.amount) }}</small></button></div>
      </div>
      <div class="foot-tip"><span>ⓘ</span> 星图自动包含所有可见的已导入账单；时间/分类选择只筛选展示。人工拖拽仅调整独立星图账本，不修改原始流水或第一轮报表。退款按已确认累计金额抵扣，外部额度分裂按剩余金额计算。</div>
    </div>

    <div v-if="newGroupOpen" class="modal-backdrop" @click.self="newGroupOpen = false"><form class="galaxy-modal" @submit.prevent="saveGroup"><span class="modal-eyebrow">STRUCTURE EDITOR</span><h2>创建星系分组</h2><p>新建分组后，可以将交易拖到这里。所有变更均可撤销。</p><label>分组名称</label><input v-model="groupName" required maxlength="50" placeholder="例如：餐饮 / 咖啡 / 旅行" autofocus/><label>上级分组</label><select v-model="groupParent"><option v-if="tree" :value="tree.root_id">时间区间 · 顶层分类</option><option v-for="n in groups" :key="n.id" :value="n.id">{{ n.name }}</option></select><label>聚合方式</label><select v-model="groupOp"><option value="ADD">ADD · 直接相加</option><option value="NET">NET · 正负净额</option></select><div class="modal-actions"><button type="button" @click="newGroupOpen = false">取消</button><button type="submit" :disabled="busy">创建分组</button></div></form></div>
  </div>
</template>

<style scoped>
.nebula-page { color:#e8edff; width:100%; max-width:1800px; margin:0 auto; font-family:Inter,-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; }
button,select,input { font:inherit; }
button { cursor:pointer; transition:background .18s,color .18s,transform .18s,border-color .18s; }
button:disabled { cursor:not-allowed; opacity:.3; }
.nebula-heading {display:flex;justify-content:space-between;align-items:center;gap:18px;padding:17px 4px 24px;color:#14233a;}
.eyebrow {display:flex;align-items:center;gap:8px;color:#707ca0;font-size:10px;letter-spacing:2.25px;font-weight:800;}
.pulse-dot {width:7px;height:7px;border-radius:50%;background:#51c8c5;box-shadow:0 0 0 4px #c9f3ec;}
.eyebrow-index{color:#afb9cc;font-weight:500;}
.nebula-heading h1 {font-size:39px;line-height:1.3;margin:6px 0 5px;letter-spacing:-1px;font-weight:780;}
.nebula-heading h1 span:first-child{color:#5486d1;}.heading-star{font-size:22px;vertical-align:top;margin-left:8px;color:#9a82e1;}
.nebula-heading p {font-size:13px;color:#8693a9;}
.heading-actions{display:flex;align-items:center;flex-wrap:wrap;gap:9px;}
.book-control,.btn-muted,.btn-primary{height:39px;border:1px solid #dce4f1;border-radius:10px;display:flex;align-items:center;justify-content:center;gap:9px;padding:0 14px;background:#fff;color:#50617d;font-size:12px;font-weight:650;}
.book-control{padding:0 10px 0 14px;}.book-control select{border:0;outline:0;background:transparent;color:#354962;font-weight:700;max-width:180px;cursor:pointer;}.book-dot{font-size:15px;color:#7787d7;}
.btn-primary{color:#fff;background:linear-gradient(135deg,#4369d5,#7b7fda);border:0;box-shadow:0 8px 24px #6782d94a;}.btn-muted:hover{background:#f5f8ff;}.btn-primary:hover{transform:translateY(-1px);}
.message {margin:0 0 12px;padding:11px 15px;border-radius:12px;display:flex;justify-content:space-between;gap:12px;color:#214e40;background:#e9f9f1;font-size:12px;}
.message-error{background:#ffefed;color:#b62d37;}.message button{background:transparent;border:0;font-size:17px;color:inherit;}
.metrics {display:grid;grid-template-columns:1fr 1fr 1fr .9fr;gap:13px;margin-bottom:14px;}
.metric{min-width:0;border:1px solid #e5eafa;border-radius:16px;padding:21px 22px 19px;background:linear-gradient(140deg,#fff,#fcfdff);box-shadow:0 8px 28px #52608c0b;}
.metric-label{font-weight:680;color:#64748d;font-size:12px;display:flex;align-items:center;justify-content:space-between;}.m-orbit{color:#adbde1;font-size:18px;font-weight:400;}
.metric-value{font-size:clamp(20px,2.2vw,29px);line-height:1.8;color:#22304c;font-weight:760;letter-spacing:-.9px;white-space:nowrap;}
.metric-value.mint{color:#169c89}.metric-value.salmon{color:#d97765}.unit{font-size:13px;color:#8a94a8;font-weight:500;letter-spacing:0;}
.metric-sub{color:#a9b1c2;font-size:10px;letter-spacing:1.7px;display:flex;gap:10px;align-items:center;}
.metric-pill{padding:3px 7px;letter-spacing:.4px;border-radius:5px;font-size:10px;font-weight:600;}.coral{background:#fff1ee;color:#cc8c7c;}.cyan{background:#e3f8f7;color:#44aaa8;}
.galaxy-shell {border:1px solid #26385f;background:#080f24;border-radius:19px;box-shadow:0 25px 70px #12274c37;overflow:hidden;position:relative;}
.galaxy-toolbar{height:63px;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:0 24px;background:linear-gradient(95deg,#101c34,#121936);border-bottom:1px solid #ffffff13;position:relative;z-index:5;}
.tabs{display:flex;align-items:center;background:#ffffff08;border-radius:10px;padding:4px;gap:3px;}
.tabs button{padding:9px 15px;border:0;border-radius:7px;background:transparent;color:#93a1c2;font-size:12px;font-weight:650;white-space:nowrap;}.tabs button.active{background:#6573c5;color:#fff;box-shadow:0 3px 14px #5668d158;}.tabs button:hover:not(.active){color:#fff;background:#ffffff0d;}
.galaxy-tools{display:flex;align-items:center;gap:8px;color:#9cabd0;}.time-range{font-size:11px;white-space:nowrap;letter-spacing:.2px;}.tool-separator{height:20px;width:1px;background:#ffffff1f;margin:0 7px;}
.galaxy-tools button{border:1px solid #ffffff19;background:#ffffff09;color:#adbde3;display:grid;place-items:center;width:31px;height:31px;border-radius:8px;font-size:20px;line-height:1;}.galaxy-tools button:hover{background:#697aca33;color:#fff;}
.universe{position:relative;height:clamp(680px,70vw,900px);min-height:680px;background:radial-gradient(ellipse 37% 47% at 53% 54%,#24315a99 0%,#131c38a1 46%,transparent 100%),radial-gradient(ellipse 33% 48% at 7% 42%,#113e5888,transparent 100%),radial-gradient(ellipse 35% 47% at 90% 44%,#392d6580,transparent 100%),#090f23;overflow:hidden;}
.nebula-aurora{pointer-events:none;position:absolute;filter:blur(62px);opacity:.27;width:33%;height:43%;border-radius:50%;top:30%;}.nebula-aurora-left{left:-10%;background:#2da6bf;}.nebula-aurora-right{right:-9%;background:#7257d7;}
.galaxy-svg{display:block;width:100%;height:100%;touch-action:none;cursor:grab;position:relative;z-index:1;user-select:none;}.galaxy-svg:active{cursor:grabbing;}
.orbit-rings circle{fill:none;stroke:#b7bcf1;stroke-opacity:.075;stroke-width:1;stroke-dasharray:4 8;}.orbit-rings circle:nth-child(2){stroke-dasharray:1 12;stroke-opacity:.14;}.orbit-rings circle:nth-child(3){stroke-opacity:.05;}
.node-core{fill:#1b2a47;stroke:#7fc3e0;stroke-width:2;transition:stroke .18s,fill .18s;}.node-core-inner{fill:#54a8ce;opacity:.11;}
.node-halo{fill:#4eadd3;opacity:.26;}.side-category .node-core{fill:#2d2456;stroke:#ad90f7;}.side-category .node-core-inner{fill:#a780ee;}.side-category .node-halo{fill:#a38afc;opacity:.2;}
.kind-root .node-core{fill:#101d3d;stroke:url(#rootGradient);stroke-width:4;}.kind-root .node-core-inner{fill:#313977;opacity:.4;}.kind-root .node-halo{fill:#767de2;opacity:.39;}
.kind-lens .node-core{stroke-width:3;}.kind-tx .node-core{fill:#d2a46d;stroke:#ffce95;stroke-width:1;}.kind-tx .node-halo{fill:#edaf78;opacity:.14;}.kind-tx .node-core-inner{fill:#ffdead;opacity:.8;}
.g-node{cursor:pointer;}.g-node:hover .node-core{stroke:#fff;stroke-width:3;}.g-node.selected-node .node-core{stroke:#eaf7ff;stroke-width:3.3;}.g-node.drop-target .node-core{fill:#317e7e;stroke:#ffe1ac;stroke-width:4;}.g-node.tx-draggable{cursor:grab;}.g-node.tx-draggable:active{cursor:grabbing;}
.root-orbit{transform-origin:0 0;animation:orbit-spin 40s linear infinite;}@keyframes orbit-spin{to{transform:rotate(360deg)}}
.root-icon,.root-name,.root-count,.lens-icon,.group-icon,.expand-glyph{text-anchor:middle;pointer-events:none;user-select:none;}.root-icon{font-size:27px;fill:#94ddeb;}.root-name{fill:#f1f6ff;font-size:23px;font-weight:740;letter-spacing:7px;}.root-count{fill:#94a4c8;font-size:10px;letter-spacing:2px;}.lens-icon{fill:#c4dcff;font-size:31px;}.group-icon{fill:#d6e9fc;font-size:16px;}
.node-label{pointer-events:none;user-select:none;paint-order:stroke;stroke:#11182d;stroke-width:3.5px;stroke-linejoin:round;}.label-title{fill:#d9e6fb;font-size:15px;font-weight:650;letter-spacing:.15px;}.label-value{fill:#98a9cb;font-size:12px;font-weight:450;}.kind-tx .label-title{font-size:12px;font-weight:530;}.kind-tx .label-value{font-size:11px;}.kind-lens .label-title{fill:#fff;font-size:18px;font-weight:780;}.expand-dot{fill:#5764a8;stroke:#bec4f9;stroke-width:1;}.expand-glyph{fill:#fff;font-size:14px;}
.orbit-label{position:absolute;top:25px;display:flex;align-items:baseline;gap:9px;z-index:2;color:#89cee1;font-size:11px;letter-spacing:1.9px;font-weight:650;pointer-events:none;}.orbit-label span{border:1px solid #5f9ebd88;padding:4px 6px;border-radius:4px;font-size:9px;}.orbit-label small{color:#6f90aa;font-size:10px;letter-spacing:.4px;}.orbit-left{left:24px;}.orbit-right{right:25px;color:#b5a0f4;}.orbit-right span{border-color:#9784c98a;}.orbit-right small{color:#918cba;}
.canvas-hint{position:absolute;z-index:3;top:62px;left:23px;color:#8da2c2;font-size:11px;letter-spacing:.25px;pointer-events:none;}.universe-loading{position:absolute;inset:0;z-index:3;display:flex;justify-content:center;align-items:center;gap:14px;color:#c4d7ff;letter-spacing:1px;}.loader-ring{width:25px;height:25px;border:2px solid #ffffff30;border-top-color:#7be0e0;border-radius:50%;animation:spin .9s linear infinite;}@keyframes spin{to{transform:rotate(360deg)}}
.zoom-controls{position:absolute;z-index:3;left:23px;bottom:58px;display:flex;align-items:center;gap:4px;background:#162341eb;border:1px solid #ffffff1c;border-radius:10px;padding:5px;box-shadow:0 10px 30px #0002;}.zoom-controls button{border:0;background:transparent;color:#c9d5fa;font-size:20px;width:28px;height:28px;border-radius:5px;}.zoom-controls button:hover{background:#ffffff1a;}.zoom-controls span{font-size:10px;color:#aebde0;min-width:35px;text-align:center;}
.legend{position:absolute;z-index:3;left:22px;bottom:17px;display:flex;gap:17px;align-items:center;font-size:10px;color:#9eadcd;pointer-events:none;}.legend>span{display:inline-flex;align-items:center;gap:6px;}.legend i{width:7px;height:7px;border-radius:50%;display:inline-block;}.legend-time{background:#69c6d7;box-shadow:0 0 11px #59d7e0;}.legend-category{background:#aa85e9;box-shadow:0 0 11px #9e77e6;}.legend-tx{background:#ffc483;box-shadow:0 0 11px #f6bb6d;}.legend-fine{opacity:.65;}
.inspector{position:absolute;z-index:4;right:22px;bottom:23px;width:283px;max-height:71%;overflow-y:auto;color:#dfe7fc;border:1px solid #afc2fa35;background:linear-gradient(145deg,#1a2543f2,#121c33f7);backdrop-filter:blur(24px);box-shadow:0 20px 60px #05091b77;border-radius:14px;scrollbar-color:#5e6882 transparent;scrollbar-width:thin;}
.inspector-top{display:flex;align-items:center;justify-content:space-between;padding:15px 17px;border-bottom:1px solid #ffffff13;font-size:10px;font-weight:750;letter-spacing:1.45px;color:#9eaee0;}.inspector-top small{font-size:10px;letter-spacing:.1px;color:#7c8dac;font-weight:400;margin-left:5px;}.inspector-top button{color:#afbcdd;border:0;background:transparent;font-size:21px;line-height:1;}
.inspector-content{padding:19px 17px 20px;}.inspect-kind{font-size:9px;color:#81d3cf;letter-spacing:2px;font-weight:650;margin-bottom:7px;}.inspector h3{font-size:18px;line-height:1.5;color:#f3f6ff;font-weight:680;overflow-wrap:anywhere;}.inspect-total{font-size:24px;font-weight:760;letter-spacing:-.4px;color:#eef3ff;margin:10px 0 18px;}.inspect-stats{display:grid;gap:10px;}.inspect-stats>div{display:flex;justify-content:space-between;gap:10px;padding-bottom:10px;border-bottom:1px solid #ffffff15;font-size:11px;color:#8ea0c4;}.inspect-stats b{color:#dae5ff;font-weight:650;}
.inspect-note{font-size:11px;color:#7f91b6;line-height:1.65;margin-top:16px;}.inspect-amount{font-size:24px;color:#f8c692;font-weight:760;margin:12px 0 18px;}.inspect-line{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #ffffff13;font-size:11px;line-height:1.6;}.inspect-line span{color:#92a2c3;white-space:nowrap;}.inspect-line b{color:#dce4f8;text-align:right;font-weight:550;overflow-wrap:anywhere;}
.reassign-panel,.op-panel{margin-top:18px;display:grid;gap:9px;font-size:11px;color:#aebde1;}.reassign-panel select,.galaxy-modal select,.galaxy-modal input{height:37px;border:1px solid #ffffff30;background:#223250;border-radius:8px;color:#e3edff;max-width:100%;padding:0 9px;outline:0;}.reassign-panel select option{background:#15223a;}.action-btn{padding:10px 12px;border:0;color:#fff;font-weight:650;background:#6777d2;border-radius:8px;}.action-btn:hover{background:#7f8be2;}.op-panel{grid-template-columns:1fr auto;align-items:center;}.op-panel button{padding:6px 12px;border:1px solid #718ac280;background:#263755;color:#b4e4de;border-radius:6px;font-size:11px;}.op-panel button.net{color:#fad38f;border-color:#d0a86b66;}
.reopen-inspector{position:absolute;z-index:4;right:23px;bottom:20px;color:#c3d1ef;background:#182746ed;border:1px solid #ffffff20;padding:11px;border-radius:9px;font-size:12px;}.drag-label{font-size:13px;fill:#ffe9bd;paint-order:stroke;stroke:#091126;stroke-width:3px;}
.galaxy-footer{height:49px;background:#111b33;border-top:1px solid #ffffff14;display:flex;align-items:center;justify-content:space-between;padding:0 23px;font-size:11px;color:#90a0c4;letter-spacing:.1px;}.audit-mark{display:inline-grid;place-items:center;background:#1a705c;color:#b0ffe0;border-radius:50%;width:17px;height:17px;margin-right:7px;font-size:11px;}.sep{opacity:.4;margin:0 9px;}.galaxy-footer button{border:0;background:transparent;color:#a0b4e0;text-decoration:underline;text-underline-offset:4px;}
.below-galaxy{display:flex;gap:18px;justify-content:space-between;align-items:center;padding:14px 3px 8px;}.search-box{position:relative;display:flex;align-items:center;background:#fff;border:1px solid #e0e6f2;border-radius:10px;padding:0 12px;height:42px;min-width:320px;box-shadow:0 5px 19px #142a5008;}.search-box>span{font-size:22px;color:#8799b4;margin-right:9px;}.search-box input{flex:1;width:100%;border:0;outline:0;color:#283957;font-size:12px;}.search-box input::placeholder{color:#acb8c9;}.search-box>button{border:0;background:transparent;color:#8a9bb0;font-size:18px;}.search-results{position:absolute;z-index:10;top:47px;left:0;width:min(425px,85vw);background:#fff;border:1px solid #e6e9f2;border-radius:12px;box-shadow:0 18px 35px #16294f26;max-height:320px;overflow:auto;}.search-results button{width:100%;padding:12px 15px;display:flex;justify-content:space-between;text-align:left;gap:15px;border:0;background:white;color:#344461;font-size:12px;}.search-results button:hover{background:#f0f5ff;}.search-results small{color:#8a9ab1;white-space:nowrap;}.foot-tip{font-size:11px;color:#8a98ad;line-height:1.7;max-width:850px;}.foot-tip>span{color:#6a85b3;font-size:15px;margin-right:4px;}
.modal-backdrop{position:fixed;inset:0;background:#080d1bc2;backdrop-filter:blur(4px);z-index:40;display:grid;place-items:center;padding:16px;}.galaxy-modal{width:min(100%,435px);background:#172340;border:1px solid #ffffff36;border-radius:18px;padding:29px 30px;display:grid;gap:12px;color:#e6edff;box-shadow:0 26px 100px #0007;}.modal-eyebrow{color:#77d5cd;font-size:11px;font-weight:750;letter-spacing:2px;}.galaxy-modal h2{font-size:24px;font-weight:740;margin-top:-3px;}.galaxy-modal p{line-height:1.65;color:#9faccc;font-size:12px;margin-bottom:8px;}.galaxy-modal label{font-size:11px;color:#b5c1dc;font-weight:650;margin-top:5px;}.galaxy-modal input,.galaxy-modal select{height:41px;}.galaxy-modal select option{background:#172340;}.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:14px;}.modal-actions button{padding:11px 18px;border:1px solid #ffffff2c;border-radius:9px;background:#253451;color:#ced9f6;font-size:12px;}.modal-actions button[type=submit]{background:#6977d3;border:0;color:#fff;font-weight:700;}
@media(max-width:1050px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr));}.nebula-heading{flex-wrap:wrap;}.time-range{display:none;}.inspector{width:260px;}.universe{height:740px;}.foot-tip{display:none;}}
@media(max-width:660px){.nebula-heading h1{font-size:30px;}.heading-actions{width:100%;}.btn-primary,.btn-muted{flex:1;}.book-control{flex:1}.metrics{gap:7px}.metric{padding:14px}.metric-value{font-size:19px}.metric-sub{font-size:8px}.galaxy-toolbar{padding:0 8px;gap:4px;height:56px}.tabs button{font-size:10px;padding:8px}.galaxy-tools{gap:3px}.galaxy-tools button{font-size:17px;width:24px;height:29px}.tool-separator{display:none}.universe{height:710px;min-height:0}.inspector{max-height:47%;width:calc(100% - 40px);right:20px;bottom:60px}.legend{gap:9px;left:11px}.legend-fine{display:none}.zoom-controls{left:12px}.orbit-label{top:16px}.orbit-left{left:12px}.orbit-right{right:12px}.orbit-label small{display:none}.canvas-hint{left:12px;top:51px;font-size:10px;max-width:70%}.galaxy-footer{padding:0 10px;font-size:9px}.search-box{width:100%;min-width:0}}
@media(prefers-reduced-motion:reduce){.root-orbit,.loader-ring{animation:none}}

/* Automatic live view controls */
.live-indicator{display:inline-flex;align-items:center;gap:9px;color:#246d75;background:#ebfaf8;border:1px solid #c1ebe5;border-radius:10px;padding:10px 13px;font-size:12px;font-weight:680;white-space:nowrap}
.live-indicator .pulse-dot{height:7px;width:7px;box-shadow:0 0 0 4px #c5eee8}
.star-filters{display:flex;align-items:end;flex-wrap:wrap;gap:11px;background:#f9fbff;border:1px solid #e1e8f5;padding:15px 17px;margin:0 0 16px;border-radius:15px;box-shadow:0 8px 18px #162b450a}
.filter-field{display:flex;flex-direction:column;gap:6px;min-width:105px}
.filter-field label{font-size:10px;color:#687b95;letter-spacing:.3px;font-weight:700}
.filter-field select,.filter-field input{border:1px solid #d5deed;min-height:36px;border-radius:8px;background:white;color:#273c5c;padding:0 10px;font-size:12px;max-width:220px}
.filter-period{min-width:120px}.filter-category{min-width:170px;flex:1;max-width:270px}.filter-category select{width:100%;max-width:100%}.filter-depth{min-width:105px}
.filter-reset{min-height:36px;border:1px solid #dbe4f1;border-radius:9px;background:#fff;color:#5e72a0;font-size:11px;padding:0 12px}
.filter-summary{margin-left:auto;color:#5a6d8c;font-size:11px;padding:10px 0;white-space:nowrap}
.empty-ledger{position:absolute;top:47%;left:50%;z-index:5;transform:translate(-50%,-50%);max-width:400px;width:85%;padding:25px;border:1px solid #7f9edb66;background:#142140ed;border-radius:15px;text-align:center;color:#eaf2ff;box-shadow:0 22px 75px #0008}
.empty-ledger h3{font-size:18px;margin:0 0 12px}.empty-ledger p{font-size:12px;line-height:1.8;color:#9daed0}
.empty-ledger button{padding:10px 17px;border:none;background:#687de0;color:white;border-radius:9px;margin-top:17px}
.empty-ledger small{display:block;font-size:10px;color:#8290b3;overflow-wrap:anywhere;margin-top:15px}
@media(max-width:700px){.star-filters{gap:8px;padding:10px}.filter-field{min-width:0;flex:1 1 100px}.filter-category{flex:2 1 180px}.filter-summary{margin-left:0}.live-indicator{font-size:10px;padding:8px}}

/* V2.2 · immersive progressive drill-down. Canvas is deliberately ~10% shorter. */
.nebula-page { max-width: 1660px; }
.universe { height: clamp(680px, 66vw, 845px); min-height: 680px; }
.pathbar { position:relative; z-index:4; display:flex; align-items:center; gap:14px; min-height:53px; padding:9px 20px; border-bottom:1px solid #ffffff19; background:#101a31; color:#ccd9f8; }
.path-back {flex-shrink:0; border:1px solid #869ee05c; background:#283a62; border-radius:9px; color:#ecf2ff; padding:8px 14px; font-size:12px; font-weight:650; }
.path-back:hover:not(:disabled) {background:#455b90;}
.path-back:disabled {opacity:.38;cursor:default;}
.path-crumbs {display:flex;align-items:center;gap:6px;min-width:0;overflow-x:auto;scrollbar-width:thin;white-space:nowrap;}
.path-crumbs button {background:transparent;border:0;color:#98abd1;padding:6px 4px;font-size:12px;border-radius:4px;white-space:nowrap;}
.path-crumbs button:hover {color:white;background:#ffffff0e;}
.path-crumbs button.current {color:#8de0ef;font-weight:750;}
.crumb-divider{color:#4c5c7b;font-size:13px;}
.path-depth {color:#7d92b9;font-size:11px;margin-left:auto;white-space:nowrap;}
.focus-pages {display:flex;align-items:center;gap:8px;margin-left:auto;color:#9bb5dd;white-space:nowrap;font-size:11px;}
.focus-pages button {color:#d7e5ff;background:#24355a;border:1px solid #ffffff1b;border-radius:7px;width:27px;height:27px;font-size:17px;}
.focus-pages button:hover:not(:disabled){background:#435a8f;}
.quick-date {display:flex;flex-direction:column;gap:6px;}
.quick-date>label {font-size:10px;font-weight:700;color:#687b95;letter-spacing:.3px;}
.date-segments {display:flex;gap:3px;align-items:center;background:#eef2f9;border:1px solid #e3eafa;border-radius:9px;padding:3px;}
.date-segments button {border:0;border-radius:7px;color:#687a96;background:transparent;padding:7px 12px;font-size:11px;font-weight:600;}
.date-segments button.active {color:#fff;background:#637cc6;box-shadow:0 3px 8px #243d782b;}
.date-segments button:hover:not(.active){background:white;color:#2b466b;}
.date-cascade {display:flex;align-items:center;gap:5px;min-height:36px;}
.date-cascade select {height:35px;border:1px solid #d5deed;border-radius:8px;background:#fff;color:#273c5c;padding:0 8px;font-size:11px;}
.date-arrow {width:29px;height:35px;border:1px solid #d5deed;border-radius:8px;background:white;color:#48669a;font-size:23px;line-height:1;}
.date-arrow:hover:not(:disabled){background:#eef5ff;}
.filter-hint{align-self:end;margin-bottom:10px;color:#7c8da8;font-size:11px;}
/* SVG scene transition: selected cluster moves into the center while the old universe fades away. */
.drill-enter-active,.drill-leave-active {transition: opacity .23s ease, transform .23s cubic-bezier(.22,.7,.25,1);transform-box:view-box;transform-origin:0 0;pointer-events:none;}
.drill-enter-from {opacity:0;transform:scale(.78);}
.drill-leave-to {opacity:0;transform:scale(1.19);}
.zoom-out .drill-enter-from {transform:scale(1.14);}
.zoom-out .drill-leave-to {transform:scale(.8);}
.focus-center .root-name {font-size:13px;}
@media(max-width:1050px) {.universe {height:730px;min-height:640px;} .pathbar{gap:8px;padding:8px 10px;} .path-back{padding:7px 9px;} }
@media(max-width:660px) {.universe {height:640px;min-height:560px;} .date-segments button{padding:7px 9px;} .path-depth{display:none;} .focus-pages span{font-size:10px;} }
@media(prefers-reduced-motion:reduce){.drill-enter-active,.drill-leave-active{transition:none;}}

.drag-targets {position:absolute;z-index:8;right:24px;top:90px;display:flex;flex-direction:column;gap:6px;width:205px;max-height:55%;overflow-y:auto;padding:13px;background:#101d3bee;border:1px solid #8eacff75;border-radius:13px;box-shadow:0 16px 45px #0008;}
.drag-targets strong {color:#b9d8ff;font-size:11px;margin-bottom:4px;}
.drag-targets button {border:1px solid #677aa54d;background:#293a61;color:#e3ecff;text-align:left;border-radius:8px;padding:10px 11px;font-size:12px;}
.drag-targets button.highlight {background:#156d78;border-color:#7fe1db;box-shadow:0 0 0 2px #8de8de33;}
.drag-targets button:disabled {opacity:.35;}


/* V2.3 · synchronized companion galaxy and hover-to-drill interaction */
.preview-node { cursor: pointer; opacity: .92; }
.preview-node:hover, .hovered-node { opacity: 1; }
.hovered-node .node-core { stroke: #ffffff; stroke-width: 2.2px; filter: drop-shadow(0 0 10px rgba(132,208,255,.55)); }
.hovered-node .node-halo { opacity: .95; }
.preview-node .node-label .label-title { fill: #d5e4ff; }
.preview-node .node-label .label-value { fill: #8fa8d4; }


/* V2.4: Local semantic zoom. Wheel exposes nearby satellites without changing focus. */
.node-layer .satellite-node { cursor: pointer; }
.satellite-node .node-halo { opacity: .72; }
.satellite-node .node-core { stroke: #a8caff; stroke-width: 1.8; filter: drop-shadow(0 0 10px #8ed6fa88); }
.satellite-node.side-category .node-core { stroke: #ceafff; }
.satellite-node .label-title { fill: #eef6ff; font-weight: 700; }
.satellite-node .label-value { fill: #a8c9e8; }
.satellite-edge { stroke-dasharray: 4 7; opacity: .83; }
.satellite-more .node-core { opacity: .67; stroke-dasharray: 3 3; }
.satellite-enter-active, .satellite-leave-active { transition: opacity .34s cubic-bezier(.2,.75,.25,1), filter .34s ease; }
.satellite-enter-from { opacity: 0; filter: blur(8px); }
.satellite-leave-to { opacity: 0; filter: blur(8px); }
@media (prefers-reduced-motion: reduce) {
  .satellite-enter-active, .satellite-leave-active { transition: none; }
}

/* V3 · 固定世界坐标：画布比原版短，页面更容易滚动。 */
.nebula-page { max-width: 1480px; }
.universe { height: clamp(560px, 58vw, 720px); min-height: 560px; }
.star-map-heading {color:#e4edff;font-size:13px;font-weight:680;display:flex;align-items:center;gap:9px;white-space:nowrap;}
.star-map-heading span {color:#82d9e5;font-size:16px;}
.star-map-heading small {font-size:10px;font-weight:400;color:#91a4ca;margin-left:14px;}
.star-status {min-height:34px;display:flex;align-items:center;gap:20px;padding:8px 20px;background:#101a31;color:#92a8cf;font-size:10px;border-bottom:1px solid #ffffff19;}
.star-status span:last-child {margin-left:auto;color:#7390b6;}
@media (max-width:1050px) { .universe {height:540px;min-height:500px;} .star-map-heading small{display:none;} }
@media (max-width:660px) { .universe {height:470px;min-height:430px;} .star-status span:last-child{display:none;} .star-map-heading{font-size:11px;} }
</style>
