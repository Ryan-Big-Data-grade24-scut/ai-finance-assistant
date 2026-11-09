import { computed, ref, type ComputedRef, type Ref } from 'vue'
import type { LedgerOperation, LedgerTx } from '@/api/ledger'
import type { CameraPan, GNode, Point, Edge } from './useStarGraph'

export interface StarInteractionOptions {
  zoom: Ref<number>
  pan: Ref<CameraPan>
  hoveredId: Ref<string>
  semanticAnchorId: Ref<string>
  graph: ComputedRef<{nodes:Point[];edges:Edge[]}>
  graphById: ComputedRef<Map<string,Point>>
  getCanvas: () => {svg:SVGSVGElement|null;scene:SVGGElement|null}
  enterFocus: (id:string)=>void
  focusTx: (tx:LedgerTx)=>void
  applyCompanionSelection: (node:GNode)=>void
  runOp: (op:LedgerOperation)=>Promise<void>
}

// Zoom thresholds deliberately do not navigate. Only a click invokes enterFocus.
// Keep 0.10 hysteresis at the first boundary to prevent flicker from trackpads.
export function semanticZoomDepth(scale:number):number {
  if(scale>=2.04)return 3
  if(scale>=1.52)return 2
  if(scale>=1.06)return 1
  return 0
}
export function useStarInteraction(options:StarInteractionOptions) {
  const {zoom,pan,hoveredId,semanticAnchorId,graph,graphById,getCanvas,
    enterFocus,focusTx,applyCompanionSelection,runOp}=options
  const revealLevel=computed(()=>semanticAnchorId.value?semanticZoomDepth(zoom.value):0)
  const dragLocation=ref<{x:number;y:number}|null>(null)
  const dropId=ref('')
  const dropPanel=ref<HTMLDivElement|null>(null)
  let pointer:null|{kind:'pan'|'tx';x:number;y:number;panX:number;panY:number;tx?:LedgerTx;moved:boolean}=null

  function getWorld(event:PointerEvent):CameraPan {
    const {svg,scene}=getCanvas()
    if(!svg||!scene)return {x:0,y:0}
    const point=svg.createSVGPoint();point.x=event.clientX;point.y=event.clientY
    const matrix=scene.getScreenCTM()
    if(!matrix)return {x:0,y:0}
    const world=point.matrixTransform(matrix.inverse())
    return {x:world.x,y:world.y}
  }
  function onCanvasDown(event:PointerEvent) {
    if(event.button!==0)return
    pointer={kind:'pan',x:event.clientX,y:event.clientY,panX:pan.value.x,panY:pan.value.y,moved:false}
    getCanvas().svg?.setPointerCapture(event.pointerId)
  }
  function startNode(event:PointerEvent,p:Point) {
    if(event.button!==0)return
    event.stopPropagation()
    if(p.node.peekMore){
      const anchor=graphById.value.get(p.node.peekParentId??'')
      if(anchor?.node.preview)applyCompanionSelection(anchor.node)
      else if(anchor)enterFocus(anchor.node.id)
      return
    }
    if(p.node.preview){applyCompanionSelection(p.node);return}
    if(p.node.kind==='tx'&&p.node.side==='category'){
      pointer={kind:'tx',x:event.clientX,y:event.clientY,panX:0,panY:0,moved:false,tx:p.node.tx}
      dragLocation.value={x:p.x,y:p.y}
      getCanvas().svg?.setPointerCapture(event.pointerId)
    }else enterFocus(p.node.id)
  }
  function onMove(event:PointerEvent) {
    if(!pointer)return
    const dx=event.clientX-pointer.x,dy=event.clientY-pointer.y
    if(Math.hypot(dx,dy)>8)pointer.moved=true
    if(pointer.kind==='pan'){
      if(!pointer.moved)return
      const r=getCanvas().svg?.getBoundingClientRect()
      const factor=r?Math.max(1920/r.width,1080/r.height):1
      pan.value={x:pointer.panX+dx*factor,y:pointer.panY+dy*factor}
    }else if(pointer.kind==='tx'&&pointer.moved){
      dragLocation.value=getWorld(event)
      let best='',distance=92
      for(const p of graph.value.nodes){
        if(p.node.preview)continue
        if(p.node.side!=='category'||!['group','lens','placeholder'].includes(p.node.kind)||!p.node.nodeId)continue
        const d=Math.hypot(p.x-dragLocation.value.x,p.y-dragLocation.value.y)
        if(d<distance){distance=d;best=p.node.nodeId}
      }
      for(const element of dropPanel.value?.querySelectorAll<HTMLElement>('[data-target]')??[]){
        const box=element.getBoundingClientRect()
        if(event.clientX>=box.left&&event.clientX<=box.right&&event.clientY>=box.top&&event.clientY<=box.bottom){
          best=element.dataset.target??'';break
        }
      }
      dropId.value=best
    }
  }
  function onPointerUp() {
    if(pointer?.kind==='tx'&&pointer.tx){
      if(pointer.moved&&dropId.value&&dropId.value!==pointer.tx.node_id){
        void runOp({op:'reassign',tx_id:pointer.tx.id,to_node_id:dropId.value})
      }else if(!pointer.moved)focusTx(pointer.tx)
    }
    pointer=null;dragLocation.value=null;dropId.value=''
  }
  function zoomBy(step:number){
    zoom.value=Math.max(.28,Math.min(2.7,Number((zoom.value+step).toFixed(2))))
    if(zoom.value<.94)semanticAnchorId.value=''
  }
  function zoomAtPointer(event:WheelEvent,next:number){
    const {svg,scene}=getCanvas()
    const svgMatrix=svg?.getScreenCTM(),sceneMatrix=scene?.getScreenCTM()
    if(!svg||!scene||!svgMatrix||!sceneMatrix){zoom.value=next;return}
    const p=svg.createSVGPoint();p.x=event.clientX;p.y=event.clientY
    const inViewport=p.matrixTransform(svgMatrix.inverse()),inWorld=p.matrixTransform(sceneMatrix.inverse())
    pan.value={x:inViewport.x-next*inWorld.x,y:inViewport.y-next*inWorld.y}
    zoom.value=next
  }
  function onWheel(event:WheelEvent){
    if(pointer?.kind==='tx')return
    const amount=event.deltaMode===1?event.deltaY*16:event.deltaMode===2?event.deltaY*280:event.deltaY
    const next=Math.max(.28,Math.min(2.7,Number((zoom.value+(amount<0?.075:-.075)).toFixed(3))))
    // Do not invoke enterFocus/goBack here: wheel only changes local visual detail.
    if(amount<0&&next>=1.06){
      const hovered=graphById.value.get(hoveredId.value)
      if(hovered&&hovered.hasChildren&&hovered.depth>0&&!hovered.satellite&&hovered.node.kind!=='tx'){
        semanticAnchorId.value=hovered.node.id
      }
    }
    zoomAtPointer(event,next)
    if(next<.94)semanticAnchorId.value=''
  }
  function resetLocal(){semanticAnchorId.value='';hoveredId.value=''}
  return {revealLevel,dragLocation,dropId,dropPanel,onCanvasDown,startNode,onMove,onPointerUp,onWheel,zoomBy,resetLocal}
}
