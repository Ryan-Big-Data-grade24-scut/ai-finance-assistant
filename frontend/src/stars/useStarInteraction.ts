import { ref, type ComputedRef, type Ref } from 'vue'
import type { LedgerOperation, LedgerTx } from '@/api/ledger'
import type { CameraPan, Point, Edge } from './useStarGraph'

export interface StarInteractionOptions {
  zoom: Ref<number>; pan: Ref<CameraPan>; hoveredId: Ref<string>
  graph: ComputedRef<{nodes:Point[];edges:Edge[]}>
  getCanvas: () => {svg:SVGSVGElement|null;scene:SVGGElement|null}
  focusPoint: (point:Point)=>void
  runOp: (op:LedgerOperation)=>Promise<void>
}
export function useStarInteraction({zoom,pan,hoveredId,graph,getCanvas,focusPoint,runOp}:StarInteractionOptions) {
  const dragLocation=ref<{x:number;y:number}|null>(null)
  const dropId=ref('')
  const dropPanel=ref<HTMLDivElement|null>(null)
  let pointer:null|{kind:'pan'|'tx';x:number;y:number;panX:number;panY:number;tx?:LedgerTx;moved:boolean}=null
  let skipClick=false
  function worldPoint(event:PointerEvent):CameraPan {
    const {svg,scene}=getCanvas()
    if(!svg||!scene)return {x:0,y:0}
    const pt=svg.createSVGPoint();pt.x=event.clientX;pt.y=event.clientY
    const matrix=scene.getScreenCTM()
    if(!matrix)return {x:0,y:0}
    const pos=pt.matrixTransform(matrix.inverse())
    return {x:pos.x,y:pos.y}
  }
  function begin(event:PointerEvent, point?:Point){
    if(event.button!==0)return
    skipClick=false
    if(point?.node.kind==='tx') {
      pointer={kind:'tx',x:event.clientX,y:event.clientY,panX:0,panY:0,tx:point.node.tx,moved:false}
      dragLocation.value={x:point.x,y:point.y}
    }else pointer={kind:'pan',x:event.clientX,y:event.clientY,panX:pan.value.x,panY:pan.value.y,moved:false}
    ;(point ? event.currentTarget as Element : getCanvas().svg)?.setPointerCapture(event.pointerId)
  }
  function onCanvasDown(event:PointerEvent){begin(event)}
  function startNode(event:PointerEvent,point:Point){event.stopPropagation();begin(event,point)}
  function onMove(event:PointerEvent){
    if(!pointer)return
    const dx=event.clientX-pointer.x,dy=event.clientY-pointer.y
    if(Math.hypot(dx,dy)>8)pointer.moved=true
    if(pointer.kind==='pan') {
      if(!pointer.moved)return
      const svg=getCanvas().svg
      const bounds=svg?.getBoundingClientRect()
      const box=svg?.viewBox.baseVal
      const ratio=bounds&&box?Math.min(bounds.width/box.width,bounds.height/box.height):1
      pan.value={x:pointer.panX+dx/ratio,y:pointer.panY+dy/ratio}
    }else if(pointer.moved){
      dragLocation.value=worldPoint(event)
      let target='',distance=100
      for(const p of graph.value.nodes){
        if(p.node.kind==='tx'||p.node.peekMore||!p.node.nodeId)continue
        const d=Math.hypot(p.x-dragLocation.value.x,p.y-dragLocation.value.y)
        if(d<distance){distance=d;target=p.node.nodeId}
      }
      for(const element of dropPanel.value?.querySelectorAll<HTMLElement>('[data-target]')??[]){
        const r=element.getBoundingClientRect()
        if(event.clientX>=r.left&&event.clientX<=r.right&&event.clientY>=r.top&&event.clientY<=r.bottom){target=element.dataset.target??'';break}
      }
      dropId.value=target
    }
  }
  function onPointerUp(){
    if(pointer?.moved)skipClick=true
    if(pointer?.kind==='tx'&&pointer.tx&&pointer.moved&&dropId.value&&dropId.value!==pointer.tx.node_id)
      void runOp({op:'reassign',tx_id:pointer.tx.id,to_node_id:dropId.value})
    pointer=null;dragLocation.value=null;dropId.value=''
  }
  function shouldIgnoreClick(){return skipClick}
  function zoomBy(step:number){zoom.value=Math.max(.36,Math.min(3.2,Number((zoom.value+step).toFixed(3))))}
  function onWheel(event:WheelEvent){
    if(pointer?.kind==='tx')return
    const {svg,scene}=getCanvas()
    const delta=event.deltaMode===1?event.deltaY*16:event.deltaMode===2?event.deltaY*250:event.deltaY
    const next=Math.max(.36,Math.min(3.2,Number((zoom.value*(delta<0?1.095:.915)).toFixed(3))))
    const svgMatrix=svg?.getScreenCTM(),sceneMatrix=scene?.getScreenCTM()
    if(!svg||!scene||!svgMatrix||!sceneMatrix){zoom.value=next;return}
    const pt=svg.createSVGPoint();pt.x=event.clientX;pt.y=event.clientY
    const target=pt.matrixTransform(svgMatrix.inverse()),world=pt.matrixTransform(sceneMatrix.inverse())
    pan.value={x:target.x-world.x*next,y:target.y-world.y*next}
    zoom.value=next
  }
  function resetLocal(){hoveredId.value='';skipClick=false}
  // Exposed for keyboard/search selection, not for page navigation.
  function centerOn(point:Point){focusPoint(point)}
  return {dragLocation,dropId,dropPanel,onCanvasDown,startNode,onMove,onPointerUp,onWheel,zoomBy,resetLocal,shouldIgnoreClick,centerOn}
}
