<script setup lang="ts">
/**
 * 跨页面账本助手 ATLAS。
 *
 * 和 /v2/assistant 共用同一个会话（localStorage 的 agentcore.session）与同一份
 * 页面快照（agentPageState）；只是界面更紧凑。收起浮窗不停止正在运行的一轮。
 *
 * 公仔可以拖到视口任意位置，位置和形象都记在 localStorage；面板跟着公仔走，
 * 也可以按住面板标题单独拖动。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AgentCoreChat from '@/components/AgentCoreChat.vue'
import AtlasMascot from '@/components/AtlasMascot.vue'
import { useAgentSession } from '@/composables/useAgentSession'
import { buildAgentPageContext } from '@/utils/agentPageContext'
import { agentPageState } from '@/utils/agentPageState'

const route = useRoute()
const open = ref(false)
const started = ref(false)

const {
  input, detail, opening, sending, stopping, reconnectText, errorBanner, notice,
  run, active, items, send, stop, init,
} = useAgentSession({
  pageContext: () => buildAgentPageContext({
    route: agentPageState.route || route.path,
    surface: 'pet',
    dateFrom: agentPageState.dateFrom,
    dateTo: agentPageState.dateTo,
    selection: agentPageState.selection,
  }),
  canSend: () => !opening.value && !sending.value,
})

const contextLabel = computed(() => {
  const parts: string[] = []
  if (agentPageState.dateFrom && agentPageState.dateTo) {
    parts.push(`${agentPageState.dateFrom} ~ ${agentPageState.dateTo}`)
  }
  if (agentPageState.selection.length) parts.push(`已选 ${agentPageState.selection.length} 项`)
  return parts.join(' · ')
})

// ── 公仔形象 ─────────────────────────────────────────────────────────────

const VARIANT_NAMES = ['星仪', '账灵', '夜枭']
const variant = ref(restoreVariant())

function restoreVariant(): number {
  try {
    const raw = Number(localStorage.getItem('atlas.mascot'))
    return Number.isInteger(raw) && raw >= 0 && raw < VARIANT_NAMES.length ? raw : 0
  } catch {
    return 0
  }
}
function cycleVariant() {
  variant.value = (variant.value + 1) % VARIANT_NAMES.length
  try { localStorage.setItem('atlas.mascot', String(variant.value)) } catch { /* 隐私模式 */ }
}

const petState = computed(() => active.value ? 'thinking' : open.value ? 'open' : 'idle')

// ── 位置：公仔与面板 ─────────────────────────────────────────────────────

const PET_W = 78
const PET_H = 104
const PET_KEY = 'atlas.position'
const PANEL_KEY = 'atlas.panel'

const viewport = ref({ w: window.innerWidth, h: window.innerHeight })
const panelW = computed(() => Math.min(392, viewport.value.w - 24))
const panelH = computed(() => Math.min(560, Math.round(viewport.value.h * 0.72)))
const pos = ref(restorePet())
const panelPos = ref<{ left: number; top: number } | null>(restorePanel())

function clamp(left: number, top: number, w: number, h: number) {
  const vw = viewport.value.w
  const vh = viewport.value.h
  return {
    left: Math.min(Math.max(6, left), Math.max(6, vw - w - 6)),
    top: Math.min(Math.max(6, top), Math.max(6, vh - h - 6)),
  }
}
function clampPet(left: number, top: number) {
  return clamp(left, top, PET_W, PET_H)
}
function clampPanel(left: number, top: number) {
  return clamp(left, top, panelW.value, panelH.value)
}

function restorePet(): { left: number; top: number } {
  let saved: { left?: number; top?: number } | null = null
  try {
    const raw = localStorage.getItem(PET_KEY)
    if (raw) saved = JSON.parse(raw)
  } catch { saved = null }
  if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
    return clampPet(Number(saved.left), Number(saved.top))
  }
  return clampPet(window.innerWidth - PET_W - 22, window.innerHeight - PET_H - 20)
}
function restorePanel(): { left: number; top: number } | null {
  try {
    const raw = localStorage.getItem(PANEL_KEY)
    if (!raw) return null
    const saved = JSON.parse(raw) as { left?: number; top?: number }
    return Number.isFinite(saved.left) && Number.isFinite(saved.top)
      ? clampPanel(Number(saved.left), Number(saved.top))
      : null
  } catch {
    return null
  }
}
function savePet() {
  try { localStorage.setItem(PET_KEY, JSON.stringify(pos.value)) } catch { /* 隐私模式 */ }
}
function savePanel() {
  if (!panelPos.value) return
  try { localStorage.setItem(PANEL_KEY, JSON.stringify(panelPos.value)) } catch { /* 隐私模式 */ }
}

/** 没手动拖过面板时，面板贴着公仔打开；上方放不下就放下方，上下都放不下就放到旁边。 */
const anchoredPanel = computed(() => {
  const vw = viewport.value.w
  const vh = viewport.value.h
  const rightHalf = pos.value.left + PET_W / 2 >= vw / 2
  let left = rightHalf ? pos.value.left + PET_W - panelW.value : pos.value.left
  let top = pos.value.top - panelH.value - 10
  if (top < 6) top = pos.value.top + PET_H + 8
  if (top + panelH.value > vh - 6) {
    // 公仔贴底时上下都放不下面板：改为左右并排，避免面板盖住公仔（公仔仍可拖动）。
    top = pos.value.top + PET_H / 2 - panelH.value / 2
    left = rightHalf ? pos.value.left - panelW.value - 12 : pos.value.left + PET_W + 12
  }
  return clampPanel(left, top)
})
const panelPlacement = computed(() => panelPos.value ?? anchoredPanel.value)
const panelStyle = computed(() => ({
  left: `${panelPlacement.value.left}px`,
  top: `${panelPlacement.value.top}px`,
  width: `${panelW.value}px`,
  height: `${panelH.value}px`,
}))

// ── 拖动 ─────────────────────────────────────────────────────────────────

type Drag = { id: number; sx: number; sy: number; left: number; top: number; moved: boolean }
let petDrag: Drag | null = null
let panelDrag: Drag | null = null
let suppressClick = false
const draggingPet = ref(false)
const draggingPanel = ref(false)

function petDown(event: PointerEvent) {
  if (event.button !== 0) return
  petDrag = { id: event.pointerId, sx: event.clientX, sy: event.clientY, left: pos.value.left, top: pos.value.top, moved: false }
  draggingPet.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function petMove(event: PointerEvent) {
  if (!petDrag || event.pointerId !== petDrag.id) return
  const dx = event.clientX - petDrag.sx
  const dy = event.clientY - petDrag.sy
  if (!petDrag.moved && Math.hypot(dx, dy) > 4) petDrag.moved = true
  if (!petDrag.moved) return
  const before = pos.value
  pos.value = clampPet(petDrag.left + dx, petDrag.top + dy)
  if (panelPos.value) {
    // 面板被单独拖过：公仔移动时面板保持相对位置。
    panelPos.value = clampPanel(
      panelPos.value.left + pos.value.left - before.left,
      panelPos.value.top + pos.value.top - before.top,
    )
  }
}
function petUp(event: PointerEvent) {
  if (!petDrag || event.pointerId !== petDrag.id) return
  const moved = petDrag.moved
  petDrag = null
  draggingPet.value = false
  if (moved) {
    savePet()
    suppressClick = true
    window.setTimeout(() => { suppressClick = false }, 0)
  }
}
function onPetClick() {
  if (suppressClick) return
  void toggle()
}

function headDown(event: PointerEvent) {
  if (event.button !== 0) return
  if ((event.target as Element).closest('button, a')) return
  const start = panelPlacement.value
  panelDrag = { id: event.pointerId, sx: event.clientX, sy: event.clientY, left: start.left, top: start.top, moved: false }
  draggingPanel.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function headMove(event: PointerEvent) {
  if (!panelDrag || event.pointerId !== panelDrag.id) return
  const dx = event.clientX - panelDrag.sx
  const dy = event.clientY - panelDrag.sy
  if (!panelDrag.moved && Math.hypot(dx, dy) > 4) panelDrag.moved = true
  if (!panelDrag.moved) return
  panelPos.value = clampPanel(panelDrag.left + dx, panelDrag.top + dy)
}
function headUp(event: PointerEvent) {
  if (!panelDrag || event.pointerId !== panelDrag.id) return
  if (panelDrag.moved) savePanel()
  panelDrag = null
  draggingPanel.value = false
}

function onResize() {
  viewport.value = { w: window.innerWidth, h: window.innerHeight }
  pos.value = clampPet(pos.value.left, pos.value.top)
  if (panelPos.value) panelPos.value = clampPanel(panelPos.value.left, panelPos.value.top)
}

async function toggle() {
  open.value = !open.value
  if (open.value && !started.value) {
    started.value = true
    await init()
  }
}

function closeOnEscape(event: KeyboardEvent) {
  if (event.key === 'Escape') open.value = false
}
window.addEventListener('keydown', closeOnEscape)
window.addEventListener('resize', onResize)
onMounted(() => { onResize() })
onBeforeUnmount(() => {
  window.removeEventListener('keydown', closeOnEscape)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <div class="fa">
    <section v-if="open" class="fa-panel" :style="panelStyle" :class="{ 'fa-panel--drag': draggingPanel }"
             data-testid="floating-assistant" aria-label="ATLAS 账本助手浮窗">
      <header class="fa-head" @pointerdown="headDown" @pointermove="headMove"
              @pointerup="headUp" @pointercancel="headUp">
        <div class="fa-title">
          <b>ATLAS</b>
          <small v-if="contextLabel" data-testid="floating-context">{{ contextLabel }}</small>
        </div>
        <button class="fa-look" title="换个形象" data-testid="floating-look" @click="cycleVariant">
          ↻ {{ VARIANT_NAMES[variant] }}
        </button>
        <RouterLink to="/v2/assistant" class="fa-link" data-testid="floating-full">完整页面</RouterLink>
        <button class="fa-close" aria-label="收起助手" data-testid="floating-close" @click="open = false">×</button>
      </header>
      <AgentCoreChat
        class="fa-chat"
        compact
        :show-header="false"
        v-model:input="input"
        :items="items"
        :run="run"
        :active="active"
        :status="detail?.status ?? ''"
        :step="detail?.step ?? 0"
        :totals="detail?.totals ?? null"
        :sending="sending"
        :stopping="stopping"
        :opening="opening"
        :reconnect-text="reconnectText"
        :error-banner="errorBanner"
        :notice="notice"
        placeholder="问流水，或让它按当前范围改 V2…"
        @send="send"
        @stop="stop"
      />
    </section>

    <button class="fa-pet" :class="{ 'fa-pet--on': open, 'fa-pet--drag': draggingPet }"
            :style="{ left: `${pos.left}px`, top: `${pos.top}px` }"
            data-testid="floating-toggle" :aria-expanded="open"
            aria-label="ATLAS 账本助手（拖动可移动，点击打开）"
            @pointerdown="petDown" @pointermove="petMove"
            @pointerup="petUp" @pointercancel="petUp" @click="onPetClick">
      <span class="fa-figure"><AtlasMascot :variant="variant" :state="petState" /></span>
      <span class="fa-name">ATLAS</span>
      <span v-if="active" class="fa-dot" aria-label="正在运行" />
    </button>
  </div>
</template>

<style scoped>
.fa { position: static; z-index: 50; }

/* 公仔：整块都是拖动区，点击（没有拖动）时开合面板。 */
.fa-pet {
  position: fixed; z-index: 51;
  width: 78px; height: 104px; padding: 0 5px 4px;
  display: flex; flex-direction: column; align-items: center; justify-content: flex-end;
  border: 0; background: transparent; cursor: grab; touch-action: none; user-select: none;
  filter: drop-shadow(0 10px 18px rgba(28, 66, 108, .22));
  transition: transform .16s ease;
}
.fa-pet:hover { transform: translateY(-2px); }
.fa-pet--drag { cursor: grabbing; transform: scale(1.03); }
.fa-pet--on { filter: drop-shadow(0 10px 24px rgba(28, 66, 108, .34)); }
.fa-figure { width: 68px; height: 82px; }
.fa-name {
  margin-top: 1px; padding: 1px 9px; border-radius: 999px;
  background: linear-gradient(145deg, #2f67a3, #5a90cc); color: #fff;
  font-size: 10px; font-weight: 700; letter-spacing: .08em;
  box-shadow: 0 3px 8px rgba(47, 103, 163, .28);
}
.fa-dot {
  position: absolute; right: 9px; top: 10px;
  width: 9px; height: 9px; border-radius: 50%; background: #46d68c;
  box-shadow: 0 0 0 3px rgba(70, 214, 140, .25);
  animation: fa-pulse 1.4s ease-in-out infinite;
}
@keyframes fa-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.35); }
}

.fa-panel {
  position: fixed; z-index: 50;
  display: flex; flex-direction: column; overflow: hidden;
  background: #fff; border: 1px solid var(--color-border); border-radius: 16px;
  box-shadow: 0 18px 46px rgba(25, 58, 93, .22);
}
.fa-panel--drag { box-shadow: 0 22px 56px rgba(25, 58, 93, .3); }
.fa-head {
  flex: none; display: flex; align-items: center; gap: 8px;
  padding: 9px 11px; border-bottom: 1px solid var(--color-border); background: #fbfdff;
  cursor: move; touch-action: none;
}
.fa-title { flex: 1; min-width: 0; }
.fa-title b { font-size: 13px; letter-spacing: .06em; }
.fa-title small {
  display: block; margin-top: 2px; color: var(--color-muted); font-size: 10px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.fa-look {
  flex: none; padding: 3px 8px; border: 1px solid #dbe6f2; border-radius: 999px;
  background: #f4f8fd; color: #40628a; font-size: 10px; cursor: pointer;
}
.fa-look:hover { background: #e8f1fb; }
.fa-link { color: #2f6fae; font-size: 11px; text-decoration: none; white-space: nowrap; }
.fa-close {
  width: 24px; height: 24px; border: 0; border-radius: 50%; cursor: pointer;
  background: transparent; color: var(--color-muted); font-size: 18px; line-height: 1;
}
.fa-close:hover { background: #eef4fa; color: var(--color-text); }
.fa-chat { flex: 1; min-height: 0; }

@media (max-width: 560px) {
  .fa-name { display: none; }
  .fa-look { display: none; }
}
</style>
