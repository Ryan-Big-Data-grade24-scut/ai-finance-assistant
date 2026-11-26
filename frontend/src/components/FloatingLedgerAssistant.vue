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
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AgentCoreChat from '@/components/AgentCoreChat.vue'
import AgentSessionSpinner from '@/components/AgentSessionSpinner.vue'
import AtlasIcon from '@/components/AtlasIcon.vue'
import AtlasMascot from '@/components/AtlasMascot.vue'
import { useAgentSession } from '@/composables/useAgentSession'
import { buildAgentPageContext } from '@/utils/agentPageContext'
import { agentPageState } from '@/utils/agentPageState'

const route = useRoute()
const open = ref(false)
const petButton = ref<HTMLButtonElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)

const {
  draft, skills, skillStatus, refreshSkills,
  detail, currentId, opening, sending, stopping, reconnectText, errorBanner, notice,
  sessions, sessionBusy, run, active, items, isSessionRunning, openSession, newSession, removeCurrent,
  send, stop, init,
} = useAgentSession({
  pageContext: () => buildAgentPageContext({
    route: agentPageState.route || route.path,
    surface: 'pet',
    dateFrom: agentPageState.dateFrom,
    dateTo: agentPageState.dateTo,
    selection: agentPageState.selection,
  }),
})

function chooseSession(event: Event) {
  const selector = event.target as HTMLSelectElement
  const id = selector.value
  selector.value = currentId.value ?? ''
  if (id) void openSession(id)
}

const contextLabel = computed(() => {
  const parts: string[] = []
  if (agentPageState.dateFrom || agentPageState.dateTo) {
    parts.push(`${agentPageState.dateFrom || '不限'} 至 ${agentPageState.dateTo || '不限'}`)
  }
  if (agentPageState.selection.length) parts.push(`已选 ${agentPageState.selection.length} 项`)
  return parts.join(' · ')
})

// ── 公仔形象 ─────────────────────────────────────────────────────────────

const VARIANTS = [
  { name: '科技少年', style: '帅气' },
  { name: '星光少女', style: '可爱' },
]
const variant = ref(restoreVariant())
const lookOpen = ref(false)
const lookControl = ref<HTMLElement | null>(null)
const lookButton = ref<HTMLButtonElement | null>(null)
const looks = ref<HTMLElement | null>(null)

function restoreVariant(): number {
  try {
    const raw = Number(localStorage.getItem('atlas.mascot'))
    return Number.isInteger(raw) && raw >= 0 && raw < VARIANTS.length ? raw : 0
  } catch {
    return 0
  }
}
function closeLook(restoreFocus = false) {
  if (!lookOpen.value) return
  lookOpen.value = false
  if (restoreFocus) lookButton.value?.focus({ preventScroll: true })
}
function selectVariant(index: number) {
  variant.value = index
  try { localStorage.setItem('atlas.mascot', String(index)) } catch { /* 隐私模式 */ }
  closeLook(true)
}
async function toggleLook() {
  if (lookOpen.value) {
    closeLook()
    return
  }
  lookOpen.value = true
  await nextTick()
  looks.value?.querySelector<HTMLElement>('[aria-pressed="true"]')?.focus({ preventScroll: true })
}
function onLookOutside(event: Event) {
  if (event.target instanceof Node && !lookControl.value?.contains(event.target)
      && !looks.value?.contains(event.target)) closeLook()
}
function onLookKeys(event: KeyboardEvent) {
  const buttons = Array.from(looks.value?.querySelectorAll('button') ?? [])
  const index = buttons.findIndex(button => button === document.activeElement)
  const offset = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0
  if (!buttons.length || !offset) return
  event.preventDefault()
  buttons[(index + offset + buttons.length) % buttons.length]?.focus()
}

const petState = computed(() => active.value ? 'thinking' : open.value ? 'open' : 'idle')

// ── 位置：公仔与面板 ─────────────────────────────────────────────────────

const PET_W = 96
const PET_H = 106
const PET_KEY = 'atlas.position'
const PANEL_KEY = 'atlas.panel'

const viewport = ref({ w: window.innerWidth, h: window.innerHeight })
const panelW = computed(() => Math.min(416, viewport.value.w - 24))
const panelH = computed(() => Math.min(590, viewport.value.h - 24, Math.max(318, Math.round(viewport.value.h * 0.73))))
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
  return clampPet(window.innerWidth - PET_W - 22, window.innerHeight - PET_H - 18)
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
    // 上下都放不下时，优先在公仔旁边打开。
    top = pos.value.top + PET_H / 2 - panelH.value / 2
    left = rightHalf ? pos.value.left - panelW.value - 12 : pos.value.left + PET_W + 12
  }
  // 窄屏给面板两侧留相同的边距，单独拖动时仍以用户的位置为准。
  if (vw <= 440) left = (vw - panelW.value) / 2
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
    savePanel()
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
  if ((event.target as Element).closest('button, a, .fa-looks')) return
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

function closePanel() {
  closeLook()
  open.value = false
  petButton.value?.focus({ preventScroll: true })
}

async function toggle() {
  if (open.value) {
    closePanel()
    return
  }
  open.value = true
  void init()
  await nextTick()
  if (open.value && document.activeElement === petButton.value) closeButton.value?.focus({ preventScroll: true })
}

function closeOnEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || event.isComposing || !open.value) return
  if (lookOpen.value) {
    closeLook(true)
    return
  }
  closePanel()
}
onMounted(() => {
  onResize()
  window.addEventListener('keydown', closeOnEscape)
  window.addEventListener('pointerdown', onLookOutside, true)
  window.addEventListener('focusin', onLookOutside)
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', closeOnEscape)
  window.removeEventListener('pointerdown', onLookOutside, true)
  window.removeEventListener('focusin', onLookOutside)
  window.removeEventListener('resize', onResize)
})
</script>

<template>
  <div class="fa atlas-theme">
    <section v-if="open" id="atlas-floating-panel" class="fa-panel" :style="panelStyle"
             :class="{ 'fa-panel--drag': draggingPanel }"
             data-testid="floating-assistant" aria-label="ATLAS 账本助手浮窗">
      <header class="fa-head" @pointerdown="headDown" @pointermove="headMove"
              @pointerup="headUp" @pointercancel="headUp">
        <div class="fa-title">
          <span class="fa-mark"><AtlasMascot :variant="variant" :state="petState" /></span>
          <span><b>ATLAS</b><small role="status">{{ active ? '正在处理' : '账本助手' }}</small></span>
        </div>
        <div class="fa-actions">
          <div ref="lookControl" class="fa-look-control">
            <button ref="lookButton" class="fa-action fa-look" type="button"
                    :aria-label="`外观，当前：${VARIANTS[variant].name}`" :aria-expanded="lookOpen"
                    :aria-controls="lookOpen ? 'atlas-looks' : undefined" data-testid="floating-look" @click="toggleLook">
              <AtlasIcon name="palette" /><span>外观</span>
            </button>
            <div v-if="lookOpen" ref="looks" id="atlas-looks" class="fa-looks" role="group"
                 aria-label="选择 ATLAS 外观" @keydown="onLookKeys">
              <p>助手外观</p>
              <button v-for="(look, index) in VARIANTS" :key="look.name" type="button" class="fa-look-option"
                      :aria-pressed="variant === index" @click="selectVariant(index)">
                <span class="fa-preview"><AtlasMascot :variant="index" /></span>
                <span class="fa-look-label"><b>{{ look.name }}</b><small>{{ look.style }}</small></span>
                <AtlasIcon v-if="variant === index" name="check" />
              </button>
            </div>
          </div>
          <RouterLink to="/v2/assistant" class="fa-action fa-full" aria-label="打开完整页面" data-testid="floating-full">
            <span>完整页面</span><AtlasIcon name="expand" />
          </RouterLink>
          <button ref="closeButton" type="button" class="fa-action fa-close" aria-label="收起助手" title="收起助手"
                  data-testid="floating-close" @click="closePanel"><AtlasIcon name="minus" /></button>
        </div>
      </header>
      <div class="fa-session-bar" aria-label="会话管理" role="group">
        <AtlasIcon name="chat" />
        <select class="fa-session-select" aria-label="切换会话" data-testid="floating-session-select"
                :value="currentId ?? ''" :disabled="sessionBusy || !sessions.length" @change="chooseSession">
          <option v-if="!currentId" value="" disabled>{{ opening ? '正在加载会话…' : '选择会话' }}</option>
          <option v-for="session in sessions" :key="session.id" :value="session.id">
            {{ session.title || '未命名对话' }}{{ isSessionRunning(session.id) ? ' · 正在运行' : '' }}
          </option>
        </select>
        <AgentSessionSpinner v-if="currentId && isSessionRunning(currentId)" />
        <button type="button" class="fa-action fa-session-button" title="新建会话" aria-label="新建会话"
                data-testid="floating-new-session" :disabled="sessionBusy" @click="newSession"><AtlasIcon name="plus" /></button>
        <button type="button" class="fa-action fa-session-button fa-session-delete" aria-label="删除当前会话"
                :title="currentId && isSessionRunning(currentId) ? '会话运行中，结束后可删除' : '删除当前会话'"
                data-testid="floating-delete-session"
                :disabled="sessionBusy || !currentId || isSessionRunning(currentId)" @click="removeCurrent">
          <AtlasIcon name="remove" />
        </button>
      </div>
      <div v-if="contextLabel" class="fa-context" data-testid="floating-context">
        <AtlasIcon name="ledger" /><b>账本工作台上下文</b><span>{{ contextLabel }}</span>
      </div>
      <AgentCoreChat
        class="fa-chat"
        :key="currentId ?? 'opening'"
        compact
        :show-header="false"
        v-model:draft="draft"
        :skills="skills"
        :skill-status="skillStatus"
        :items="items"
        :run="run"
        :active="active"
        :ready="!!currentId"
        :status="detail?.status ?? ''"
        :step="detail?.step ?? 0"
        :sending="sending"
        :stopping="stopping"
        :opening="opening"
        :reconnect-text="reconnectText"
        :error-banner="errorBanner"
        :notice="notice"
        placeholder="查账、核对支出，或整理账本…"
        subtitle="明确要求执行时会写入账本；结果以回执为准。"
        @send="send"
        @stop="stop"
        @refresh-skills="refreshSkills"
      >
        <template #welcome-art>
          <div class="fa-welcome-character"><AtlasMascot :variant="variant" /></div>
        </template>
      </AgentCoreChat>
    </section>

    <button ref="petButton" type="button" class="fa-pet" :class="{ 'fa-pet--drag': draggingPet }"
            :style="{ left: `${pos.left}px`, top: `${pos.top}px`, width: `${PET_W}px`, height: `${PET_H}px` }"
            :title="open ? '收起 ATLAS' : '打开 ATLAS'"
            data-testid="floating-toggle" :aria-expanded="open" :aria-controls="open ? 'atlas-floating-panel' : undefined"
            :aria-label="`ATLAS 账本助手（拖动可移动，点击${open ? '收起' : '打开'}${active ? '，正在运行' : ''}）`"
            @pointerdown="petDown" @pointermove="petMove"
            @pointerup="petUp" @pointercancel="petUp" @click="onPetClick">
      <AtlasMascot :variant="variant" :state="petState" />
    </button>
  </div>
</template>

<style scoped src="./FloatingLedgerAssistant.css"></style>
