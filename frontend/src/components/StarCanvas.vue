<script setup lang="ts">
import { ref } from 'vue'
import type { Point, Edge, GNode, CameraPan } from '@/stars/useStarGraph'
import { nodeRadius, nodeTitle, edgePath } from '@/stars/useStarGraph'

defineProps<{
  graph: {nodes:Point[];edges:Edge[]}
  pan: CameraPan
  zoom: number
  focusId: string
  focusPage: number
  mode: string
  selectedId: string
  hoveredId: string
  dropId: string
  drillDirection: 'in'|'out'
  dragLocation: {x:number;y:number}|null
}>()
const emit=defineEmits<{
  (e:'canvas-down', event:PointerEvent):void
  (e:'canvas-move', event:PointerEvent):void
  (e:'canvas-up'):void
  (e:'canvas-wheel', event:WheelEvent):void
  (e:'node-down', event:PointerEvent, point:Point):void
  (e:'node-enter', id:string):void
  (e:'node-leave', id:string):void
}>()
const svgEl=ref<SVGSVGElement|null>(null)
const sceneEl=ref<SVGGElement|null>(null)
defineExpose({getSvg:()=>svgEl.value,getScene:()=>sceneEl.value})
const compact=(n:number)=>n>=10000?`${(n/10000).toFixed(1)}万`:Number(n.toFixed(2)).toLocaleString('zh-CN')
function labelValue(node:GNode){return node.kind==='tx'?`¥${compact(node.tx?.effective_amount??node.tx?.amount??0)}`:`¥${compact(node.e+node.i)} · ${node.count} 笔`}
const particles=Array.from({length:68},(_,i)=>({
  id:i,
  x:Math.sin((i+1)*17.23)*913,
  y:Math.sin((i+1)*43.39)*502,
  r:.8+((i*7)%7)/12,
  opacity:.17+((i*9)%11)/52,
  delay:(i*0.39)%4.8,
  duration:2.8+(i%8)*.47,
  color:i%3===0?'#9be4eb':i%3===1?'#a995ec':'#e4edff',
}))
</script>
<template>
        <svg ref="svgEl" class="galaxy-svg" viewBox="-960 -540 1920 1080"
             @pointerdown="emit('canvas-down', $event)" @pointermove="emit('canvas-move', $event)" @pointerup="emit('canvas-up')" @pointercancel="emit('canvas-up')" @wheel.prevent="emit('canvas-wheel', $event)">
          <defs>
            <radialGradient id="coreGlow"><stop offset="0%" stop-color="#66dbff" stop-opacity=".31"/><stop offset="55%" stop-color="#5651ec" stop-opacity=".15"/><stop offset="100%" stop-color="#28296b" stop-opacity="0"/></radialGradient>
            <linearGradient id="rootGradient" x1="0" x2="1" y1="0" y2="1"><stop offset="0%" stop-color="#50dae3"/><stop offset="52%" stop-color="#696be8"/><stop offset="100%" stop-color="#a77bff"/></linearGradient>
            <linearGradient id="edgeTime" x1="0" x2="1"><stop stop-color="#6ad7d8" stop-opacity=".72"/><stop offset="100%" stop-color="#6cbaec" stop-opacity=".2"/></linearGradient>
            <linearGradient id="edgeCategory" x1="0" x2="1"><stop stop-color="#686be7" stop-opacity=".23"/><stop offset="100%" stop-color="#be9cf9" stop-opacity=".64"/></linearGradient>
            <filter id="nodeBloom" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="7"/></filter>
            <pattern id="starField" width="124" height="119" patternUnits="userSpaceOnUse"><circle cx="18" cy="22" r="1.1" fill="#8cadde" opacity=".35"/><circle cx="95" cy="77" r=".75" fill="#fff" opacity=".25"/><circle cx="50" cy="112" r=".7" fill="#bca7ff" opacity=".3"/></pattern>
          </defs>
          <rect x="-960" y="-540" width="1920" height="1080" fill="url(#starField)" />
          <g ref="sceneEl" :transform="`translate(${pan.x} ${pan.y}) scale(${zoom})`">
            <g class="micro-particles" pointer-events="none" aria-hidden="true">
              <circle v-for="particle in particles" :key="particle.id" :cx="particle.x" :cy="particle.y"
                :r="particle.r" :fill="particle.color" :opacity="particle.opacity"
                :style="{ animationDelay: `${particle.delay}s`, animationDuration: `${particle.duration}s` }" />
            </g>
            <g class="orbit-rings" pointer-events="none"><circle r="235"/><circle r="490"/><circle r="740"/><circle r="1000"/></g>
            <circle r="160" fill="url(#coreGlow)" pointer-events="none"/>
            <Transition name="drill" mode="out-in"><g :key="`${focusId}:${focusPage}:${mode}`" class="drill-stage">
            <TransitionGroup name="satellite" tag="g" class="edge-layer" pointer-events="none">
              <path v-for="e in graph.edges" :key="`${e.from.node.id}->${e.to.node.id}`" :d="edgePath(e)" :class="{'satellite-edge': e.to.satellite}" :stroke="e.to.side === 'time' ? 'url(#edgeTime)' : 'url(#edgeCategory)'" fill="none" :stroke-width="e.to.satellite ? 1.4 : 1.55"/>
            </TransitionGroup>
            <TransitionGroup name="orbit" tag="g" class="node-layer">
            <g v-for="p in graph.nodes" :key="p.node.id" :transform="`translate(${p.x} ${p.y})`"
               :class="['g-node', `kind-${p.node.kind}`, `side-${p.side}`, { 'focus-center': p.depth === 0, 'selected-node': selectedId === p.node.id, 'drop-target': dropId === p.node.nodeId && Boolean(dropId), 'tx-draggable': p.node.kind === 'tx' && p.node.side === 'category' && !p.node.preview, 'preview-node': p.node.preview, 'hovered-node': hoveredId === p.node.id, 'satellite-node': p.satellite, 'satellite-more': p.node.peekMore }]"
               @pointerdown="emit('node-down', $event, p)"
               @mouseenter="emit('node-enter', p.node.id)"
               @mouseleave="emit('node-leave', p.node.id)">
              <g class="planet-motion" :style="p.satellite && p.spawnFrom ? {
                '--origin-x': `${p.spawnFrom.x - p.x}px`, '--origin-y': `${p.spawnFrom.y - p.y}px`
              } : undefined">
              <circle :r="nodeRadius(p) + (p.depth === 0 ? 35 : 11)" class="node-halo" filter="url(#nodeBloom)"/>
              <circle v-if="p.depth === 0" r="80" fill="none" stroke="#84a8fb" stroke-opacity=".25" stroke-width="1" stroke-dasharray="4 9" class="root-orbit"/>
              <circle :r="nodeRadius(p)" class="node-core"/>
              <circle :r="nodeRadius(p) - (p.node.kind === 'tx' ? 3 : 6)" class="node-core-inner"/>
              <circle v-if="p.depth === 0" r="32" fill="none" stroke="url(#rootGradient)" stroke-width="1.5" stroke-opacity=".7"/>
              <template v-if="p.depth === 0">
                <text y="-13" class="root-icon">✦</text>
                <text y="12" class="root-name">{{ nodeTitle(p.node) }}</text>
                <text y="35" class="root-count">{{ p.node.kind === 'tx' ? `¥${compact(p.node.tx?.effective_amount ?? p.node.tx?.amount ?? 0)}` : `${p.node.count} RECORDS` }}</text>
              </template>
              <text v-else-if="p.node.kind === 'lens'" y="5" class="lens-icon">{{ p.side === 'time' ? '◷' : '❖' }}</text>
              <text v-else-if="p.node.kind !== 'tx'" y="5" class="group-icon">{{ p.node.op === 'NET' ? '≋' : p.node.kind === 'period' ? '◦' : '✦' }}</text>
              <g v-if="p.depth !== 0" :transform="`translate(${p.side === 'time' ? -(nodeRadius(p)+15) : (nodeRadius(p)+15)} 0)`" :text-anchor="p.side === 'time' ? 'end' : 'start'" class="node-label">
                <text class="label-title" y="-4">{{ nodeTitle(p.node) }}</text>
                <text class="label-value" y="14">{{ labelValue(p.node) }}</text>
              </g>
              <circle v-if="p.hasChildren && p.depth >= 1" :cx="p.side === 'time' ? -nodeRadius(p) + 3 : nodeRadius(p)-3" :cy="-nodeRadius(p) + 2" r="9" class="expand-dot"/>
              <text v-if="p.hasChildren && p.depth >= 1" :x="p.side === 'time' ? -nodeRadius(p)+3 : nodeRadius(p)-3" :y="-nodeRadius(p)+5" class="expand-glyph">+</text>
              </g>
            </g>
            </TransitionGroup>
            </g></Transition>
            <g v-if="dragLocation" :transform="`translate(${dragLocation.x} ${dragLocation.y})`" pointer-events="none">
              <circle r="26" fill="#fdca82" opacity=".23" filter="url(#nodeBloom)"/>
              <circle r="10" fill="#ffd19b" stroke="#fff" stroke-width="2"/>
              <text x="18" y="4" class="drag-label">拖至分类节点松开</text>
            </g>
          </g>
        </svg>
</template>
<style scoped>
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
.drill-enter-active,.drill-leave-active {transition: opacity .23s ease, transform .23s cubic-bezier(.22,.7,.25,1);transform-box:view-box;transform-origin:0 0;pointer-events:none;}
.drill-enter-from {opacity:0;transform:scale(.78);}
.drill-leave-to {opacity:0;transform:scale(1.19);}
.zoom-out .drill-enter-from {transform:scale(1.14);}
.zoom-out .drill-leave-to {transform:scale(.8);}
.preview-node { cursor: pointer; opacity: .92; }
.preview-node:hover, .hovered-node { opacity: 1; }
.hovered-node .node-core { stroke: #ffffff; stroke-width: 2.2px; filter: drop-shadow(0 0 10px rgba(132,208,255,.55)); }
.hovered-node .node-halo { opacity: .95; }
.preview-node .node-label .label-title { fill: #d5e4ff; }
.preview-node .node-label .label-value { fill: #8fa8d4; }
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
/* Smooth local orbital transitions. FLIP moves SVG elements between stable positions. */
.node-layer .orbit-move { transition: transform .52s cubic-bezier(.18,.72,.24,1); }
.orbit-enter-active,.orbit-leave-active { transition:opacity .38s ease, filter .38s ease; }
.orbit-enter-from,.orbit-leave-to {opacity:0;filter:blur(5px);}
.satellite-node .planet-motion { animation: local-orbit-arrival .55s cubic-bezier(.16,.82,.23,1) both; }
@keyframes local-orbit-arrival {from {transform:translate(var(--origin-x,0px),var(--origin-y,0px)) scale(.50);opacity:0;} to {transform:translate(0,0) scale(1);opacity:1;}}
.micro-particles circle { transform-box: fill-box; transform-origin:center; animation: stardust-pulse 3.4s ease-in-out infinite alternate; }
@keyframes stardust-pulse { 0% { opacity: .12; transform:scale(.7); } 100% {opacity:.62;transform:scale(1.35);} }
.satellite-node .node-core { animation: satellite-bloom .48s cubic-bezier(.12,.68,.28,1) both; transform-box:fill-box; transform-origin:center; }
@keyframes satellite-bloom { from {opacity:0; transform:scale(.64)} to {opacity:1;transform:scale(1)} }
@media (prefers-reduced-motion:reduce) { .micro-particles circle,.satellite-node .node-core,.satellite-node .planet-motion {animation:none;} .orbit-move,.drill-enter-active,.drill-leave-active,.satellite-enter-active,.satellite-leave-active {transition:none!important;} }

</style>
