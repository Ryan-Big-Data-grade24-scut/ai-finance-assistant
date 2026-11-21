<script setup lang="ts">
/**
 * 账本助手的对话面：消息、运行状态、输入框。
 *
 * 只负责显示和输入；会话、订阅与发送由 useAgentSession 管，
 * 这样完整页面和跨页面浮窗能共用同一个 session。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { renderMarkdown } from '@/utils/markdown'
import { stateLabel } from '@/utils/agentLabels'
import type { AgentChatItem, AgentRun, AgentTerminal, AgentToolCall } from '@/api/agentCore'

const props = withDefaults(defineProps<{
  items: AgentChatItem[]
  run: AgentRun | null
  active: boolean
  status?: string
  step?: number
  totals?: Record<string, number> | null
  sending?: boolean
  stopping?: boolean
  opening?: boolean
  reconnectText?: string
  errorBanner?: { code: string; message: string } | null
  notice?: string
  testMode?: boolean
  title?: string
  subtitle?: string
  contextLabel?: string
  placeholder?: string
  compact?: boolean
  /** 浮窗自带标题栏时关掉这里的头部。 */
  showHeader?: boolean
}>(), {
  status: '',
  step: 0,
  totals: null,
  sending: false,
  stopping: false,
  opening: false,
  reconnectText: '',
  errorBanner: null,
  notice: '',
  testMode: false,
  title: 'ATLAS',
  subtitle: '',
  contextLabel: '',
  placeholder: '例如：导入右侧账单，把这批新增流水初始化到「旅行」，再核对净额。',
  compact: false,
  showHeader: true,
})

const input = defineModel<string>('input', { default: '' })
const emit = defineEmits<{ send: []; stop: [] }>()

const composing = ref(false)
const scrollArea = ref<HTMLElement | null>(null)

const needsSettings = computed(() =>
  props.errorBanner?.code === 'NOT_CONFIGURED' || props.errorBanner?.code === 'NOT_AUTHORIZED')

const pretty = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
function callArgs(args: string) {
  try { return pretty(JSON.parse(args)) } catch { return args }
}
function resultLabel(item: Extract<AgentChatItem, { type: 'tool' }>) {
  const result = typeof item.result === 'object' ? item.result : null
  if (item.is_error) {
    const error = result?.error as { code?: string } | undefined
    if (error?.code === 'STOPPED_BEFORE_CALL' || error?.code === 'INTERRUPTED_BEFORE_CALL') return '未派发'
    if (error?.code === 'RECEIPT_UNAVAILABLE' || error?.code === 'RECEIPT_MISSING') return '回执缺失 · 待核对'
    return '失败'
  }
  if (item.name === 'finance_ledger_apply' && result?.state === 'executed') return 'V2 已执行'
  if (item.name === 'finance_import_commit' && result?.state === 'committed') return '已入库'
  return '结果'
}

// ── 工具调用与中间过程：整轮收进一条「共用时」摘要，运行中展开、结束后折叠 ──
// 手动点开/收起优先于默认值；历史轮次打开会话时保持折叠。

interface UserItem { id: number; type: 'user'; text: string; at?: string }
interface AnswerItem { id: number; type: 'assistant'; text: string; calls: AgentToolCall[]; step: number | null; at?: string }

interface ProcessBlock {
  kind: 'process'
  key: string
  items: AgentChatItem[]
  live: boolean
  label: string
}
type ChatBlock =
  | { kind: 'user'; key: string; item: UserItem }
  | { kind: 'assistant'; key: string; item: AnswerItem }
  | ProcessBlock
  | { kind: 'terminal'; key: string; item: AgentTerminal }

/** 一轮 = 一条用户消息 + 之后直到下一条用户消息的全部过程。最后一条纯文本助手消息是答案，其余进过程组。 */
const blocks = computed<ChatBlock[]>(() => {
  const out: ChatBlock[] = []
  let user: UserItem | null = null
  let turn: AgentChatItem[] = []

  const flush = () => {
    const terminal = turn.find(item => item.type === 'terminal') as AgentTerminal | undefined
    const rest = turn.filter(item => item.type !== 'terminal')
    const last = rest[rest.length - 1]
    const answer = last && last.type === 'assistant' && last.text.trim() && !last.calls.length ? last : null
    const process = answer ? rest.slice(0, -1) : rest
    if (process.length) out.push(makeProcess(process, user))
    if (answer) out.push({ kind: 'assistant', key: `a:${answer.id}`, item: answer })
    if (terminal) out.push({ kind: 'terminal', key: `t:${terminal.id}`, item: terminal })
    turn = []
    user = null
  }

  for (const item of props.items) {
    if (item.type === 'user') {
      flush()
      // user 与 compaction 共用同一个联合成员，判别式是联合类型，这里明确收窄。
      const message = item as UserItem
      user = message
      out.push({ kind: 'user', key: `u:${message.id}`, item: message })
      continue
    }
    turn.push(item)
  }
  flush()
  return out
})

function makeProcess(items: AgentChatItem[], user: UserItem | null): ProcessBlock {
  const run = props.run
  const live = Boolean(props.active && run && items.some(item => item.id >= run.start_id))
  return {
    kind: 'process',
    key: `process:${items[0].id}`,
    items,
    live,
    // 与豆包一致：结束后只留「共用时 X」，运行中说明当前进度。
    label: live
      ? (props.step ? `处理中 · 第 ${props.step} 步` : '处理中…')
      : `共用时 ${elapsedText(items, user)}`,
  }
}

function parseAt(raw?: string) {
  if (!raw) return null
  const text = raw.trim()
  // 后端用 SQLite datetime('now') 存 UTC，按浏览器本地时区显示。
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text.replace(' ', 'T')}Z`
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? null : date
}

function itemAt(item: AgentChatItem) {
  return 'at' in item ? item.at : undefined
}
function elapsedText(items: AgentChatItem[], user: UserItem | null) {
  const start = parseAt(user?.at) ?? parseAt(items.length ? itemAt(items[0]) : undefined)
  let end: Date | null = null
  for (let i = items.length - 1; i >= 0; i--) {
    end = parseAt(itemAt(items[i]))
    if (end) break
  }
  if (!start || !end) return '工作过程'
  const seconds = Math.round((end.getTime() - start.getTime()) / 1000)
  if (seconds <= 0) return '不到 1 秒'
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

const manualOpen = ref<Record<string, boolean>>({})

function processOpen(key: string, live: boolean) {
  return key in manualOpen.value ? manualOpen.value[key] : live
}
function toggleProcess(key: string, live: boolean, event: Event) {
  // 自己控制 open：原生 <details> 的切换会先改 DOM，Vue 随后又按默认值改回去。
  event.preventDefault()
  manualOpen.value = { ...manualOpen.value, [key]: !processOpen(key, live) }
}

function scrollToBottom() {
  nextTick(() => {
    if (scrollArea.value) scrollArea.value.scrollTop = scrollArea.value.scrollHeight
    trackActiveRound()
  })
}
watch(
  () => [props.items.length, props.status, props.active, props.reconnectText],
  scrollToBottom,
  { flush: 'post' },
)

// ── 轮次索引：一条用户发言算一轮；左侧短横线定位该轮，悬停预览内容 ──────────

interface RoundEntry {
  id: number
  index: number
  title: string
  snippet: string
  at: string
}

const rounds = computed<RoundEntry[]>(() => {
  const list: RoundEntry[] = []
  props.items.forEach((item, position) => {
    if (item.type !== 'user') return
    // 这一轮的摘要取其后第一条助手正文，直到下一轮开始。
    let reply = ''
    for (let i = position + 1; i < props.items.length; i++) {
      const next = props.items[i]
      if (next.type === 'user') break
      if (next.type === 'assistant' && next.text.trim()) {
        reply = next.text
        break
      }
    }
    const mine = cleanLine(item.text)
    list.push({
      id: item.id,
      index: list.length + 1,
      title: roundTitle(item.text),
      snippet: cleanLine(reply) || mine,
      at: item.at ?? '',
    })
  })
  return list
})

function cleanLine(text: string) {
  // 预览卡里不要暴露 markdown 记号；中文字符间也不能被塞进空格。
  return text.replace(/[#>*`|]+/g, '').replace(/\s+/g, ' ').trim()
}
function roundTitle(text: string) {
  const line = cleanLine(text.split('\n')[0] ?? '')
  if (!line) return '（空消息）'
  return line.length > 24 ? `${line.slice(0, 24)}…` : line
}
function roundTime(raw: string) {
  if (!raw) return ''
  const text = raw.trim()
  // 与后端一致：SQLite datetime('now') 存 UTC，按浏览器本地时区显示。
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text.replace(' ', 'T')}Z`
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const dayStart = (value: Date) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()
  const days = Math.round((dayStart(now) - dayStart(date)) / 86_400_000)
  const hm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  if (days <= 0) return `今天 ${hm}`
  if (days === 1) return `昨天 ${hm}`
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日 ${hm}`
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

const activeRoundId = ref<number | null>(null)
const hoveredRound = ref<RoundEntry | null>(null)
const cardPos = ref({ left: 0, top: 0 })
const CARD_W = 272
let scrollQueued = false

function roundAnchor(id: number) {
  return scrollArea.value?.querySelector<HTMLElement>(`[data-item-id="${id}"]`) ?? null
}

/** 当前视口顶部所在的一轮；滚动时只更新高亮，不触发重新渲染消息。 */
function trackActiveRound() {
  if (scrollQueued) return
  scrollQueued = true
  requestAnimationFrame(() => {
    scrollQueued = false
    const host = scrollArea.value
    if (!host || !rounds.value.length) {
      activeRoundId.value = null
      return
    }
    const marker = host.scrollTop + 72
    let current = rounds.value[0].id
    for (const round of rounds.value) {
      const el = roundAnchor(round.id)
      if (el && el.offsetTop <= marker) current = round.id
    }
    activeRoundId.value = current
  })
}

function jumpToRound(id: number) {
  const host = scrollArea.value
  const target = roundAnchor(id)
  if (!host || !target) return
  host.scrollTo({ top: Math.max(0, target.offsetTop - 10), behavior: 'smooth' })
  activeRoundId.value = id
}

function showRoundCard(round: RoundEntry, event: Event) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  hoveredRound.value = round
  cardPos.value = {
    left: Math.max(8, Math.min(rect.right + 10, window.innerWidth - CARD_W - 10)),
    top: Math.max(8, Math.min(rect.top - 12, window.innerHeight - 150)),
  }
}

onMounted(trackActiveRound)

function submit() {
  if (!input.value.trim()) return
  emit('send')
}
</script>

<template>
  <section class="ac-chat" :class="{ 'ac-chat--compact': compact }">
    <header v-if="showHeader" class="ac-chat-head">
      <div class="ac-chat-title">
        <h2>{{ title }}</h2>
        <p v-if="subtitle" class="muted">{{ subtitle }}</p>
        <p v-if="testMode" class="ac-warning" data-testid="ac-test-mode">测试模式 · 虚构账单 / 脚本化模型</p>
        <p v-if="contextLabel" class="ac-context" data-testid="ac-context">{{ contextLabel }}</p>
      </div>
      <div v-if="run" class="ac-run" data-testid="ac-run">
        <b>{{ run.model }}</b>
        <span>{{ stateLabel(run.state) }}<template v-if="!compact"> · 第 {{ step ?? 0 }} 步</template></span>
        <small v-if="totals && !compact" class="ac-usage">
          输入 {{ totals.input_tokens ?? 0 }} / 输出 {{ totals.output_tokens ?? 0 }}<br>
          缓存读 {{ totals.cache_read_tokens ?? 0 }} / 写 {{ totals.cache_write_tokens ?? 0 }}
        </small>
      </div>
    </header>

    <div class="ac-body">
      <div ref="scrollArea" class="ac-messages" :class="{ 'ac-messages--rail': rounds.length > 1 }"
           data-testid="ac-messages" @scroll="trackActiveRound">
        <template v-for="block in blocks" :key="block.key">
          <article v-if="block.kind === 'user'" class="ac-msg ac-msg--user" :data-item-id="block.item.id">
            <div class="ac-bubble">{{ block.item.text }}</div>
          </article>

          <!-- 一轮的全部工具调用与中间输出收进一条摘要；运行中展开，结束后只剩「共用时」。 -->
          <details v-else-if="block.kind === 'process'" class="ac-process" data-testid="ac-process"
                   :class="{ 'ac-process--live': block.live }"
                   :open="processOpen(block.key, block.live)">
            <summary @click="toggleProcess(block.key, block.live, $event)">
              <span>{{ block.label }}</span>
            </summary>
            <div class="ac-process-steps">
              <template v-for="item in block.items" :key="item.id">
                <template v-if="item.type === 'assistant'">
                  <article v-if="item.text" class="ac-msg ac-msg--assistant">
                    <div class="ac-bubble md" v-html="renderMarkdown(item.text)"></div>
                  </article>
                  <details v-for="call in item.calls" :key="call.id" class="ac-tool-result" data-testid="ac-tool-call">
                    <summary>第 {{ item.step ?? '—' }} 步 · 调用 {{ call.name }}</summary>
                    <pre>{{ callArgs(call.arguments) }}</pre>
                  </details>
                </template>
                <details v-else-if="item.type === 'tool'" class="ac-tool-result" data-testid="ac-tool-result"
                         :class="{ 'ac-tool-result--error': item.is_error }">
                  <summary>
                    {{ item.name }} · {{ resultLabel(item) }}<template v-if="item.recovered"> · 已恢复回执</template>
                  </summary>
                  <pre>{{ pretty(item.result) }}</pre>
                </details>
                <details v-else-if="item.type === 'compaction'" class="ac-tool-result">
                  <summary>已整理上下文</summary>
                  <pre>{{ item.text }}</pre>
                </details>
              </template>
            </div>
          </details>

          <article v-else-if="block.kind === 'assistant'" class="ac-msg ac-msg--assistant">
            <div class="ac-bubble md" v-html="renderMarkdown(block.item.text)"></div>
          </article>

          <p v-else class="ac-terminal" :class="{ 'ac-warning': block.item.state !== 'completed' }"
             :data-state="block.item.state" data-testid="ac-terminal">
            <b>{{ stateLabel(block.item.state) }}</b><template v-if="block.item.message"> · {{ block.item.message }}</template>
          </p>
        </template>

        <p v-if="active" class="ac-status" data-testid="ac-status" role="status">{{ status || '等待下一步…' }}</p>
        <p v-if="reconnectText" class="ac-warning" role="status">{{ reconnectText }}</p>
      </div>

      <!-- 轮次索引：一条短线一轮，点一下跳到该轮。 -->
      <nav v-if="rounds.length > 1" class="ac-rounds" aria-label="对话轮次" data-testid="ac-rounds">
        <button v-for="round in rounds" :key="round.id"
                :class="['ac-round', {
                  'ac-round--on': round.id === activeRoundId,
                  'ac-round--hover': hoveredRound?.id === round.id,
                }]"
                :aria-label="`第 ${round.index} 轮：${round.title}`"
                @click="jumpToRound(round.id)"
                @mouseenter="showRoundCard(round, $event)"
                @mouseleave="hoveredRound = null"
                @focus="showRoundCard(round, $event)"
                @blur="hoveredRound = null">
          <span class="ac-round-dash" aria-hidden="true" />
        </button>
      </nav>
    </div>

    <Teleport to="body">
      <div v-if="hoveredRound" class="ac-round-card" role="tooltip"
           :style="{ left: `${cardPos.left}px`, top: `${cardPos.top}px`, width: `${CARD_W}px` }">
        <small class="ac-round-card-no">第 {{ hoveredRound.index }} 轮</small>
        <b>{{ hoveredRound.title }}</b>
        <p>{{ hoveredRound.snippet || '还没有回复' }}</p>
        <small>{{ roundTime(hoveredRound.at) || '未记录时间' }}</small>
      </div>
    </Teleport>

    <div v-if="errorBanner" class="ac-banner" data-testid="ac-error">
      <span>{{ errorBanner.message }}</span>
      <RouterLink v-if="needsSettings" to="/ai/settings" class="ac-banner-link">去 AI 设置 →</RouterLink>
    </div>
    <p v-if="notice" class="ac-notice">{{ notice }}</p>

    <div class="ac-composer">
      <textarea v-model="input" rows="2" data-testid="ac-input" :placeholder="placeholder"
                @compositionstart="composing = true"
                @compositionend="composing = false"
                @keydown.enter.exact.prevent="!composing && submit()"></textarea>
      <button v-if="!active" class="ac-send" data-testid="ac-send"
              :disabled="!run || !input.trim() || opening || sending"
              @click="submit">
        {{ sending ? '启动中…' : '发送' }}
      </button>
      <button v-else class="ac-send ac-send--stop" data-testid="ac-stop"
              :disabled="stopping || run?.state === 'stopping'" @click="emit('stop')">
        {{ stopping || run?.state === 'stopping' ? '正在停止…' : '停止' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.ac-chat {
  display: flex; flex-direction: column; min-height: 0; height: 100%; overflow: hidden;
  background: linear-gradient(180deg, #fbfdff, #ffffff);
}
.ac-chat-head {
  flex: none; display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-2);
  padding: var(--space-3); border-bottom: 1px solid var(--color-border);
}
.ac-chat-title { min-width: 0; }
h2 { font-size: var(--font-md); }
.ac-chat-head p { margin-top: 2px; font-size: var(--font-xs); }
.muted { color: var(--color-muted); line-height: 1.6; }
.ac-context { color: #45607a; }
.ac-run {
  flex: 0 1 42%; min-width: 0; display: grid; gap: 3px; text-align: right;
  color: #45607a; font-size: var(--font-xs); overflow-wrap: anywhere;
}
.ac-usage { color: var(--color-muted); font-size: 10px; }
.ac-body { position: relative; flex: 1; min-height: 0; display: flex; }
.ac-messages {
  position: relative; flex: 1; min-width: 0; min-height: 0; overflow-y: auto;
  padding: var(--space-3); display: flex; flex-direction: column; gap: 10px;
}
.ac-messages--rail { padding-left: calc(var(--space-3) + 16px); }
/* 轮次索引：左侧一列短线，一条一轮；滚动时高亮当前轮。 */
.ac-rounds {
  position: absolute; left: 4px; top: 50%; transform: translateY(-50%); z-index: 2;
  max-height: calc(100% - 20px); overflow-y: auto; overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 9px; padding: 4px 3px;
  scrollbar-width: none;
}
.ac-rounds::-webkit-scrollbar { display: none; }
.ac-round {
  flex: none; display: flex; align-items: center; justify-content: center;
  width: 12px; height: 8px; padding: 0; border: 0; background: transparent; cursor: pointer;
}
.ac-round-dash {
  display: block; width: 8px; height: 2px; border-radius: 999px; background: #cdd8e3;
  transition: width .15s ease, background .15s ease;
}
.ac-round:hover .ac-round-dash, .ac-round--hover .ac-round-dash { width: 11px; background: #8aa8c4; }
.ac-round--on .ac-round-dash { width: 12px; background: #2f6fae; }
.ac-round:focus-visible { outline: 2px solid #8db4d9; outline-offset: 1px; border-radius: 3px; }
/* 悬停预览卡（Teleport 到 body，避免被滚动容器裁掉）。 */
.ac-round-card {
  position: fixed; z-index: 120; padding: 10px 12px;
  border: 1px solid #dbe6f2; border-radius: 10px; background: #fff;
  box-shadow: 0 12px 30px rgba(25, 58, 93, .16); pointer-events: none;
}
.ac-round-card .ac-round-card-no {
  display: block; color: #2f6fae; font-size: 10px; font-weight: 600; margin-bottom: 3px;
}
.ac-round-card b {
  display: block; font-size: 13px; line-height: 1.4;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ac-round-card p {
  margin: 5px 0 6px; color: #4a637a; font-size: 12px; line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; overflow-wrap: anywhere;
}
.ac-round-card small { color: var(--color-muted); font-size: 11px; }
.ac-msg { display: flex; }
.ac-msg--user { justify-content: flex-end; }
.ac-bubble {
  max-width: 78%; padding: 9px 12px; border-radius: 13px; line-height: 1.7;
  overflow-wrap: anywhere; font-size: var(--font-sm);
}
.ac-msg--user .ac-bubble { background: #2f6fae; color: #fff; border-bottom-right-radius: 4px; }
.ac-msg--assistant .ac-bubble { background: #f3f7fb; border: 1px solid #e3ebf3; border-bottom-left-radius: 4px; }
.ac-tool-result {
  padding: 7px 10px; border-radius: var(--radius); background: #f7fafc;
  border: 1px solid #e3ebf3; font-size: var(--font-xs); color: #45607a;
}
.ac-terminal {
  padding: 7px 0; border-top: 1px solid var(--color-border);
  font-size: var(--font-xs); color: var(--color-muted);
}
.ac-tool-result summary { cursor: pointer; }
.ac-tool-result pre {
  margin-top: 6px; padding: 8px; border-radius: var(--radius); background: #172a3b; color: #eaf2fa;
  overflow: auto; max-height: 220px; font-size: 10px; white-space: pre-wrap; overflow-wrap: anywhere;
}
.ac-tool-result--error summary { color: var(--color-expense); }

/* 一轮的过程组：默认只显示一条「共用时 X」，展开后是各步骤。 */
.ac-process { border-bottom: 1px dashed #dfe6ee; padding-bottom: 4px; }
.ac-process > summary {
  list-style: none; display: flex; align-items: center; gap: 6px;
  padding: 5px 0 6px; cursor: pointer; user-select: none;
  color: var(--color-muted); font-size: var(--font-xs);
}
.ac-process > summary::-webkit-details-marker { display: none; }
.ac-process > summary::after {
  content: '⌄'; font-size: 12px; line-height: 1; transition: transform .15s ease;
}
.ac-process[open] > summary::after { transform: rotate(180deg); }
.ac-process--live > summary { color: #2f6fae; }
.ac-process-steps { display: flex; flex-direction: column; gap: 8px; padding: 2px 0 4px; }
.ac-process .ac-tool-result { background: transparent; border: 0; padding: 0; }
.ac-process .ac-tool-result > summary { color: var(--color-muted); }
.ac-process .ac-tool-result > summary:hover { color: #2f6fae; }
.ac-process .ac-tool-result > pre { margin-top: 5px; }
.ac-status { color: var(--color-muted); font-size: var(--font-xs); }
.ac-warning { color: #9a6b00; font-size: var(--font-xs); }
.ac-banner {
  margin: 0 var(--space-3); padding: 9px 11px; border-radius: var(--radius);
  background: #fdf3f3; border: 1px solid #f2d6d7; color: #8c2f33; font-size: var(--font-xs);
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-2);
}
.ac-banner-link { color: #2f6fae; font-weight: 600; text-decoration: none; white-space: nowrap; }
.ac-notice { margin: 6px var(--space-3) 0; color: #2f7d57; font-size: var(--font-xs); }
.ac-composer {
  flex: none; display: flex; gap: var(--space-2); padding: var(--space-3);
  border-top: 1px solid var(--color-border); background: #fff;
}
.ac-composer textarea {
  flex: 1; resize: none; border: 1px solid var(--color-border); border-radius: var(--radius);
  padding: 9px 11px; font-size: var(--font-sm); line-height: 1.5; outline: none;
}
.ac-composer textarea:focus { border-color: #8db4d9; box-shadow: 0 0 0 3px rgba(54, 121, 237, .12); }
.ac-send {
  align-self: flex-end; padding: 10px 16px; border: 0; border-radius: var(--radius);
  background: #2f6fae; color: #fff; cursor: pointer; font-weight: 600;
}
.ac-send:disabled { opacity: .45; cursor: default; }
.ac-send--stop { background: #8c2f33; }

/* 浮窗里更紧凑，但消息和输入框保持同样行为。 */
.ac-chat--compact .ac-chat-head { padding: 10px 12px; }
.ac-chat--compact .ac-chat-head h2 { font-size: 14px; }
.ac-chat--compact .ac-messages { padding: 10px 12px; gap: 8px; }
.ac-chat--compact .ac-messages--rail { padding-left: 26px; }
.ac-chat--compact .ac-rounds { gap: 7px; }
.ac-chat--compact .ac-bubble { max-width: 92%; font-size: 13px; padding: 8px 10px; }
.ac-chat--compact .ac-composer { padding: 10px 12px; }
.ac-chat--compact .ac-run { flex-basis: 50%; font-size: 10px; }

/* Markdown */
.md :deep(p) { margin: 0 0 8px; }
.md :deep(p:last-child) { margin-bottom: 0; }
.md :deep(h1), .md :deep(h2), .md :deep(h3), .md :deep(h4) { margin: 10px 0 6px; line-height: 1.4; }
.md :deep(h2) { font-size: 15px; }
.md :deep(h3), .md :deep(h4) { font-size: 14px; }
.md :deep(ul), .md :deep(ol) { margin: 5px 0 9px; padding-left: 19px; }
.md :deep(li) { margin: 3px 0; }
.md :deep(code) { padding: 1px 4px; border-radius: 5px; background: #e9f0f7; font-family: ui-monospace, Consolas, monospace; font-size: .92em; }
.md :deep(pre) { overflow-x: auto; margin: 8px 0; padding: 10px; border-radius: 9px; background: #172a3b; color: #eaf2fa; }
.md :deep(pre code) { padding: 0; background: transparent; color: inherit; }
.md :deep(table) { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 11px; }
.md :deep(th), .md :deep(td) { padding: 5px 7px; border: 1px solid #e2eaf1; text-align: left; }
.md :deep(th) { background: #f1f6fb; }
.md :deep(a) { color: #2c75b4; }
</style>
