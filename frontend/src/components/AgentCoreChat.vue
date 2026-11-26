<script setup lang="ts">
/**
 * 账本助手的对话面：消息、运行状态、输入框。
 *
 * 只负责显示和输入；会话、订阅与发送由 useAgentSession 管，
 * 这样完整页面和跨页面浮窗能共用同一个 session。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { JSONContent } from '@tiptap/core'
import AgentCoreComposer from '@/components/AgentCoreComposer.vue'
import AtlasIcon from '@/components/AtlasIcon.vue'
import { readDraft, textDraft } from '@/utils/agentDraft'
import { renderMarkdown } from '@/utils/markdown'
import { stateLabel } from '@/utils/agentLabels'
import type { AgentChatItem, AgentRun, AgentSkill, AgentTerminal, AgentToolCall } from '@/api/agentCore'

const props = withDefaults(defineProps<{
  items: AgentChatItem[]
  skills: AgentSkill[]
  skillStatus: 'loading' | 'ready' | 'error'
  run: AgentRun | null
  active: boolean
  status?: string
  step?: number
  ready?: boolean
  sendBlocked?: boolean
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
  placeholder: '例如：核对十一月的出行支出，按类别列出。',
  compact: false,
  showHeader: true,
})

const draft = defineModel<JSONContent>('draft', { required: true })
const emit = defineEmits<{ send: []; stop: []; refreshSkills: [] }>()

const scrollArea = ref<HTMLElement | null>(null)
const composerInput = ref<InstanceType<typeof AgentCoreComposer> | null>(null)
const suggestions = computed(() => [
  { icon: 'ledger' as const, label: '核对支出', prompt: '帮我按类别汇总支出，列出金额和笔数，不修改账本。' },
  { icon: 'list' as const, label: '查找交易', prompt: '列出最近 5 笔支出，注明日期、分类和金额。' },
  ...(!props.compact ? [
    { icon: 'file' as const, label: '检查账单附件', prompt: '先检查已解析的附件，告诉我日期范围和笔数，暂不导入。' },
  ] : []),
])

function useSuggestion(prompt: string) {
  draft.value = textDraft(prompt)
  nextTick(() => composerInput.value?.focus())
}

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
  if (item.name === 'finance_ledger_apply' && result?.state === 'executed') return '账本工作台已执行'
  if (item.name === 'finance_import_commit' && result?.state === 'committed') return '已入库'
  if (item.name === 'finance_report_brief_write' && result?.state === 'written') return '简报已写入报告页'
  return '结果'
}

// ── 工具调用与中间过程：整轮收进一条「共用时」摘要，运行中展开、结束后折叠 ──
// 手动点开/收起优先于默认值；历史轮次打开会话时保持折叠。

type UserItem = Extract<AgentChatItem, { type: 'user' }>
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
      user = item
      out.push({ kind: 'user', key: `u:${item.id}`, item })
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
const tooltipWidth = ref(CARD_W)
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
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
  host.scrollTo({ top: Math.max(0, target.offsetTop - 10), behavior })
  activeRoundId.value = id
}

function showRoundCard(round: RoundEntry, event: Event) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  hoveredRound.value = round
  tooltipWidth.value = Math.min(CARD_W, window.innerWidth - 16)
  const left = rect.right + 10 + tooltipWidth.value <= window.innerWidth - 8
    ? rect.right + 10 : rect.left - tooltipWidth.value - 10
  cardPos.value = {
    left: Math.max(8, left),
    top: Math.max(8, Math.min(rect.top - 12, window.innerHeight - 158)),
  }
}

onMounted(scrollToBottom)

const draftParts = computed(() => readDraft(draft.value))
const canSubmit = computed(() => props.ready && !props.sendBlocked && !props.active
  && !props.opening && !props.sending && !!draftParts.value.text.trim())

function submit() {
  if (canSubmit.value) emit('send')
}
</script>

<template>
  <section class="ac-chat atlas-theme" :class="{ 'ac-chat--compact': compact }" aria-label="ATLAS 对话">
    <header v-if="showHeader" class="ac-chat-head">
      <span class="ac-brand-mark"><AtlasIcon name="atlas" /></span>
      <div class="ac-chat-title">
        <span class="ac-eyebrow">ATLAS <span aria-hidden="true">/</span> 账本助手</span>
        <h2 :title="title">{{ title }}</h2>
        <span v-if="testMode" class="ac-test" data-testid="ac-test-mode">测试模式 · 虚构账单</span>
        <p v-if="contextLabel" class="ac-context" data-testid="ac-context">{{ contextLabel }}</p>
      </div>
      <details v-if="run" class="ac-run" data-testid="ac-run">
        <summary :class="{ 'ac-run--active': active }">
          <span class="ac-run-dot" aria-hidden="true" />
          {{ stateLabel(run.state) }}<template v-if="active && step"> · 第 {{ step }} 步</template>
          <AtlasIcon name="chevron" />
        </summary>
        <div class="ac-run-meta">
          <b>{{ run.model }}</b>
          <small v-if="totals">
            输入 {{ totals.input_tokens ?? 0 }} / 输出 {{ totals.output_tokens ?? 0 }}<br>
            缓存读 {{ totals.cache_read_tokens ?? 0 }} / 写 {{ totals.cache_write_tokens ?? 0 }}
          </small>
        </div>
      </details>
    </header>

    <div class="ac-body">
      <div ref="scrollArea" class="ac-messages" :class="{ 'ac-messages--rail': rounds.length > 1 }"
           data-testid="ac-messages" @scroll="trackActiveRound">
        <div v-if="!blocks.length && !active && !errorBanner" class="ac-empty">
          <p v-if="opening" class="ac-status" role="status">正在打开对话…</p>
          <template v-else>
            <slot name="welcome-art">
              <div class="ac-constellation" aria-hidden="true">
                <svg viewBox="0 0 520 180" preserveAspectRatio="xMidYMid slice" fill="none">
                  <path d="M85 120 148 75 214 100 262 61 326 111 396 74 450 102M148 75 165 40M326 111 356 145" />
                  <circle v-for="[x, y] in [[85, 120], [148, 75], [165, 40], [214, 100], [262, 61], [326, 111], [356, 145], [396, 74], [450, 102]]"
                          :key="x" :cx="x" :cy="y" r="3" />
                </svg>
                <span class="ac-empty-mark"><AtlasIcon name="atlas" /></span>
              </div>
            </slot>
            <div class="ac-welcome">
              <span class="ac-eyebrow">ATLAS <span aria-hidden="true">/</span> 账本工作台</span>
              <h3>今天想核对哪笔账？</h3>
              <p>查流水、核对收支，或在账本工作台整理分类。</p>
              <div class="ac-suggestions" aria-label="示例问题">
                <button v-for="example in suggestions" :key="example.label" type="button"
                        :disabled="!!draftParts.text.trim() || !!draftParts.skillId || !ready || sending" @click="useSuggestion(example.prompt)">
                  <AtlasIcon :name="example.icon" />{{ example.label }}<AtlasIcon name="arrow" />
                </button>
              </div>
            </div>
          </template>
        </div>
        <template v-for="block in blocks" :key="block.key">
          <article v-if="block.kind === 'user'" class="ac-msg ac-msg--user" :data-item-id="block.item.id">
            <div class="ac-bubble"><span v-if="block.item.skill" class="ac-history-skill"
                 data-testid="ac-history-skill"><AtlasIcon name="list" />{{ block.item.skill.label }}</span><span>{{ block.item.text }}</span></div>
          </article>

          <!-- 一轮的全部工具调用与中间输出收进一条摘要；运行中展开，结束后只剩「共用时」。 -->
          <details v-else-if="block.kind === 'process'" class="ac-process" data-testid="ac-process"
                   :class="{ 'ac-process--live': block.live }"
                   :open="processOpen(block.key, block.live)">
            <summary @click="toggleProcess(block.key, block.live, $event)">
              <AtlasIcon :name="block.live ? 'list' : 'history'" />
              <span>{{ block.label }}</span>
              <AtlasIcon class="ac-chevron" name="chevron" />
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
            <div class="ac-answer-head"><AtlasIcon name="atlas" /> ATLAS</div>
            <div class="ac-bubble md" v-html="renderMarkdown(block.item.text)"></div>
          </article>

          <p v-else class="ac-terminal" :class="{ 'ac-warning': block.item.state !== 'completed' }"
             :data-state="block.item.state" data-testid="ac-terminal">
            <b>{{ stateLabel(block.item.state) }}</b><template v-if="block.item.message"> · {{ block.item.message }}</template>
          </p>
        </template>

        <p v-if="active" class="ac-status" data-testid="ac-status" role="status">
          <span class="ac-run-dot" aria-hidden="true" />{{ status || '等待下一步…' }}
        </p>
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
      <div v-if="hoveredRound" class="ac-round-card atlas-theme" role="tooltip"
           :style="{ left: `${cardPos.left}px`, top: `${cardPos.top}px`, width: `${tooltipWidth}px` }">
        <small class="ac-round-card-no">第 {{ hoveredRound.index }} 轮</small>
        <b>{{ hoveredRound.title }}</b>
        <p>{{ hoveredRound.snippet || '还没有回复' }}</p>
        <small>{{ roundTime(hoveredRound.at) || '未记录时间' }}</small>
      </div>
    </Teleport>

    <div v-if="errorBanner" class="ac-banner" data-testid="ac-error" role="alert">
      <span>{{ errorBanner.message }}</span>
      <RouterLink v-if="needsSettings" to="/ai/settings" class="ac-banner-link">去 AI 设置 <AtlasIcon name="arrow" /></RouterLink>
    </div>
    <p v-if="notice" class="ac-notice" role="status">{{ notice }}</p>

    <div class="ac-composer">
      <div class="ac-composer-box">
        <AgentCoreComposer ref="composerInput" v-model="draft" :placeholder="placeholder"
                           :skills="skills" :skill-status="skillStatus"
                           @send="submit" @refresh-skills="emit('refreshSkills')" />
        <div class="ac-composer-actions">
          <span class="ac-skill-hint"><kbd>/</kbd> 选择技能</span>
          <span class="ac-keys"><kbd>Enter</kbd> 发送 <span aria-hidden="true">·</span> <kbd>Shift + Enter</kbd> 换行</span>
          <button v-if="!active" class="ac-send" data-testid="ac-send" type="button"
                  :disabled="!canSubmit" @click="submit">
            <AtlasIcon name="send" />{{ sending ? '启动中…' : '发送' }}
          </button>
          <button v-else class="ac-send ac-send--stop" data-testid="ac-stop" type="button"
                  :disabled="stopping || run?.state === 'stopping'" @click="emit('stop')">
            <AtlasIcon name="stop" />{{ stopping || run?.state === 'stopping' ? '正在停止…' : '停止' }}
          </button>
        </div>
      </div>
      <p v-if="subtitle" class="ac-composer-note">{{ subtitle }}</p>
    </div>
  </section>
</template>

<style scoped src="./AgentCoreChat.css"></style>
