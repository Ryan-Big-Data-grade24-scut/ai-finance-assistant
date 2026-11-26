<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useLibraries } from '@/composables/useLibraries'
import type { LedgerLibrary } from '@/api/libraries'

const router = useRouter()
const { libraries, loading, error, mutating, refresh, choose, create, remove } = useLibraries()

const entering = ref('')
const createOpen = ref(false)
const newName = ref('')
const creating = ref(false)
const createError = ref('')
const createButton = ref<HTMLButtonElement | null>(null)
const nameInput = ref<HTMLInputElement | null>(null)

const deleteDialog = ref<HTMLDialogElement | null>(null)
const deleteTarget = ref<LedgerLibrary | null>(null)
const deleting = ref(false)
const deleteError = ref('')
const deleteNotice = ref('')
let deleteTrigger: HTMLElement | null = null
const busy = computed(() => mutating.value || !!entering.value || creating.value || deleting.value)

const number = new Intl.NumberFormat('zh-CN')
const summary = computed(() => [
  { label: '本账本', value: libraries.value.length },
  { label: '笔流水', value: libraries.value.reduce((sum, item) => sum + item.stats.transactions, 0) },
  { label: '笔待整理', value: libraries.value.reduce((sum, item) => sum + item.stats.uninitialized, 0) },
])

function rangeLabel(item: LedgerLibrary) {
  const { date_from: from, date_to: to } = item.stats
  if (!item.stats.readable) return '暂时无法读取流水库'
  if (!item.stats.transactions) return '还没有流水'
  if (!from && !to) return '已导入流水'
  if (from === to) return from || ''
  return `${from || '—'} 至 ${to || '—'}`
}
function stateLabel(item: LedgerLibrary) {
  if (!item.stats.readable) return { text: '无法读取', tone: 'warn' }
  if (!item.stats.transactions) return { text: '空账本', tone: 'empty' }
  if (item.stats.uninitialized > 0) return { text: `${number.format(item.stats.uninitialized)} 笔待整理`, tone: 'warn' }
  return { text: '账本已整理', tone: 'ready' }
}

async function enter(item: LedgerLibrary) {
  if (busy.value || deleteDialog.value?.open) return
  entering.value = item.id
  try {
    await choose(item.id)
    await router.push({ name: 'bill' })
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法打开账本'
  } finally {
    entering.value = ''
  }
}

async function openCreate() {
  if (busy.value || deleteDialog.value?.open) return
  createOpen.value = true
  createError.value = ''
  await nextTick()
  nameInput.value?.focus()
}
async function closeCreate() {
  if (creating.value) return
  createOpen.value = false
  await nextTick()
  createButton.value?.focus()
}
async function submitCreate() {
  const name = newName.value.trim()
  if (!name || busy.value || deleteDialog.value?.open) return
  creating.value = true
  createError.value = ''
  try {
    await create(name)
    await router.push({ name: 'bill' })
  } catch (reason) {
    createError.value = reason instanceof Error ? reason.message : '新建账本失败'
  } finally {
    creating.value = false
  }
}
function openDelete(item: LedgerLibrary, event: MouseEvent) {
  if (busy.value || !item.can_delete || deleteDialog.value?.open) return
  deleteTarget.value = item
  deleteError.value = ''
  deleteNotice.value = ''
  deleteTrigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  deleteDialog.value?.showModal()
}

function closeDelete() {
  if (!deleting.value) deleteDialog.value?.close()
}

function afterDeleteClose() {
  deleteTarget.value = null
  deleteError.value = ''
  const target = deleteTrigger?.isConnected ? deleteTrigger : createButton.value
  deleteTrigger = null
  target?.focus({ preventScroll: true })
}

async function submitDelete() {
  const item = deleteTarget.value
  if (!item || busy.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    await remove(item.id)
    deleteNotice.value = `已删除「${item.name}」。`
    deleteDialog.value?.close()
  } catch (reason) {
    deleteError.value = reason instanceof Error ? reason.message : '删除账本失败，请重试。'
    await nextTick()
    deleteDialog.value?.querySelector<HTMLElement>('[role="alert"]')?.focus()
  } finally {
    deleting.value = false
  }
}

function refreshList(force = false) {
  void refresh(force).catch(() => {})
}
onMounted(() => refreshList())
</script>

<template>
  <div class="workspace-page library-page">
    <div class="book-spread">
      <header class="title-page">
        <div class="cover-inscription">
          <span class="cover-brand" lang="en">ATLAS</span>
          <span class="cover-rule" aria-hidden="true"></span>
          <span>账本管理</span>
        </div>
        <div class="cover-center">
          <div class="compass" aria-hidden="true">
            <i class="compass-rose"></i>
          </div>
          <h1>选择一本账本</h1>
          <p>每本账本独立保存流水。打开账本后，<br class="desktop-break">就能查看报告、整理分类或导入新账单。</p>
        </div>
        <ul v-if="libraries.length" class="library-summary" aria-label="账本库概况">
          <li v-for="item in summary" :key="item.label"><b>{{ number.format(item.value) }}</b> {{ item.label }}</li>
        </ul>
      </header>
      <section class="index-page" aria-labelledby="library-heading" :aria-busy="loading">
        <div class="library-toolbar">
          <h2 id="library-heading">全部账本</h2>
          <button ref="createButton" class="workspace-button workspace-button--primary create-button"
            type="button" :aria-expanded="createOpen" :disabled="busy"
            @click="createOpen ? closeCreate() : openCreate()">
            <span aria-hidden="true">＋</span> 新建账本
          </button>
        </div>
      <form v-if="createOpen" class="workspace-panel new-ledger" @submit.prevent="submitCreate" @keydown.esc.prevent="closeCreate">
        <div>
          <h2>新建账本</h2>
          <p>先起个名字，之后再导入流水。</p>
        </div>
        <div class="new-fields">
          <label for="new-ledger-name">账本名称</label>
          <input
            id="new-ledger-name"
            ref="nameInput"
            v-model="newName"
            class="workspace-field"
            maxlength="40"
            placeholder="例如：个人日常、家庭账本"
            :disabled="creating"
          />
          <div class="new-actions">
            <button type="submit" class="workspace-button workspace-button--primary" :disabled="!newName.trim() || busy">
              {{ creating ? '正在新建…' : '新建并打开' }}
            </button>
            <button type="button" class="workspace-button" :disabled="creating" @click="closeCreate">取消</button>
          </div>
          <p v-if="createError" class="workspace-note workspace-note--error" role="alert">{{ createError }}</p>
        </div>
      </form>

      <div v-if="error" class="workspace-note workspace-note--error library-error" role="alert">
        <span>{{ error }}</span>
        <button type="button" class="workspace-button" @click="refreshList(true)">重新加载</button>
      </div>

      <p v-if="deleteNotice" class="workspace-note library-notice" role="status">{{ deleteNotice }}</p>

        <p v-if="loading && !libraries.length" class="workspace-panel library-state" role="status">正在加载账本…</p>
        <p v-else-if="!libraries.length && !error" class="workspace-panel library-state">还没有账本。先新建一本，再导入第一份账单。</p>
        <div v-else-if="libraries.length" class="ledger-grid">
          <article
            v-for="item in libraries"
            :key="item.id"
            class="ledger-card"
            :class="{ 'ledger-card--active': item.active }"
          >
            <button type="button" class="ledger-open" :disabled="busy" @click="enter(item)">
              <span class="ledger-heading">
                <span class="ledger-cover" aria-hidden="true">{{ Array.from(item.name)[0] }}</span>
                <span class="ledger-name">
                  <b>{{ item.name }}</b>
                  <small>{{ rangeLabel(item) }}</small>
                </span>
                <span v-if="item.active" class="active-pill">当前账本</span>
              </span>
              <span class="ledger-stats">
                <span><b>{{ number.format(item.stats.transactions) }}</b><small>流水</small></span>
                <span><b>{{ number.format(item.stats.initialized) }}</b><small>已归类</small></span>
                <span><b>{{ number.format(item.stats.batches) }}</b><small>导入批次</small></span>
              </span>
              <span class="ledger-foot">
                <span class="ledger-state" :class="`ledger-state--${stateLabel(item).tone}`">{{ stateLabel(item).text }}</span>
                <span class="ledger-enter">{{ entering === item.id ? '正在打开…' : '打开账本' }} <span aria-hidden="true">→</span></span>
              </span>
            </button>
            <div v-if="item.can_delete" class="ledger-actions">
              <button
                type="button"
                class="ledger-delete"
                :aria-label="`删除账本：${item.name}`"
                :disabled="busy"
                @click="openDelete(item, $event)"
              >删除</button>
            </div>
          </article>
        </div>
      </section>
    </div>

    <dialog
      ref="deleteDialog"
      class="workspace-panel delete-ledger"
      aria-labelledby="delete-ledger-heading"
      aria-describedby="delete-ledger-details delete-ledger-storage"
      @cancel.prevent="closeDelete"
      @close="afterDeleteClose"
    >
      <h2 id="delete-ledger-heading">删除账本？</h2>
      <p id="delete-ledger-details">「<b>{{ deleteTarget?.name }}</b>」将从账本列表移除，之后不能从这里打开。<template v-if="deleteTarget?.active">删除后将自动切换到另一本账本。</template></p>
      <p id="delete-ledger-storage">本地账本文件会保留，页面暂不支持恢复。</p>
      <p v-if="deleteError" class="workspace-note workspace-note--error" role="alert" tabindex="-1">{{ deleteError }}</p>
      <div class="delete-actions" :aria-busy="deleting">
        <button type="button" class="workspace-button" autofocus :disabled="deleting" @click="closeDelete">取消</button>
        <button type="button" class="workspace-button workspace-button--danger" :disabled="busy" @click="submitDelete">
          {{ deleting ? '正在删除…' : '确认删除' }}
        </button>
      </div>
    </dialog>
  </div>
</template>

<style scoped>
.library-page {
  --book-ink: #193f3a;
  --book-muted: #586661;
  --book-line: #d6cdb9;
  --book-serif: Georgia, "Songti SC", SimSun, "Noto Serif CJK SC", serif;
}
.book-spread {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, .84fr) minmax(0, 1.16fr);
  align-items: stretch;
  padding: 5px 7px 8px 5px;
  background: #203f3c;
  border-radius: 11px 19px 17px 10px;
  box-shadow: 0 3px 0 #15322e, 0 12px 20px -14px #1d302bb3, 0 28px 55px -27px #24393282;
  color: var(--book-ink);
}
.title-page {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 34px;
  min-height: clamp(485px, 65vh, 620px);
  padding: clamp(34px, 4vw, 56px) clamp(26px, 3.4vw, 48px);
  color: #fff7df;
  text-align: center;
  border-radius: 7px 2px 3px 6px;
  background:
    linear-gradient(90deg, transparent 91%, #061d1b52 99%, #0e2723),
    repeating-linear-gradient(7deg, transparent 0 3px, #dbeacd07 3px 4px),
    repeating-linear-gradient(95deg, transparent 0 2px, #e9fcdd06 2px 3px),
    radial-gradient(ellipse at 22% 15%, #35685b, #204b43 57%, #153831);
}
.title-page::before, .title-page::after {
  content: ''; position: absolute; inset: 13px; border: 1px solid #cdbd8a80;
  border-radius: 3px; pointer-events: none;
}
.title-page::after { inset: 17px; border-color: #cebb813b; }
.cover-inscription { display: flex; align-items: center; justify-content: center; gap: 12px; color: #e4d7ad; font-size: 12px; letter-spacing: .16em; }
.cover-brand { font: 600 15px Georgia, serif; letter-spacing: .24em; }
.cover-rule { width: 27px; height: 1px; background: #bcad7b; }
.cover-center h1 { font: 700 clamp(34px, 3.65vw, 55px)/1.22 var(--book-serif); letter-spacing: .045em; }
.cover-center p { margin: 17px auto 0; max-width: 30em; color: #e1e7da; font-size: 14px; line-height: 1.9; text-wrap: pretty; }
.compass {
  position: relative; display: grid; place-items: center; width: 88px; aspect-ratio: 1;
  margin: 0 auto 26px; border: 1px solid #c7b67f9c; border-radius: 50%;
  background: repeating-conic-gradient(from -1deg, #dfcc914f 0 1deg, transparent 1deg 15deg);
}
.compass::before { content: ''; position: absolute; inset: 5px; border-radius: 50%; border: 1px solid #c9b679ad; background: #295448; }
.compass-rose {
  position: absolute; width: 64%; height: 89%; background: conic-gradient(from 0deg, #f5e6b8 0 45deg, #aa945e 45deg 90deg, #f5e6b8 90deg 135deg, #aa945e 135deg 180deg, #f5e6b8 180deg 225deg, #aa945e 225deg 270deg, #f5e6b8 270deg 315deg, #aa945e 315deg);
  clip-path: polygon(50% 0,58% 41%,100% 50%,58% 59%,50% 100%,42% 59%,0 50%,42% 41%);
}
.compass::after { content: ''; position: absolute; width: 6px; height: 6px; border: 1px solid #e4ce9c; border-radius: 50%; background: #315044; }
.library-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); padding: 17px 0 0; border-top: 1px solid #c9b67c66; list-style: none; color: #d9dfcf; font-size: 12px; }
.library-summary li + li { border-left: 1px solid #c4b48250; }
.library-summary b { display: block; color: #fff5d9; font: 500 clamp(19px, 2.1vw, 30px)/1.25 Georgia, serif; font-variant-numeric: lining-nums tabular-nums; overflow-wrap: anywhere; }
.index-page {
  position: relative;
  min-width: 0;
  padding: clamp(32px, 3.7vw, 52px) clamp(23px, 3vw, 44px);
  border-radius: 2px 12px 10px 1px;
  background: linear-gradient(90deg, #cdbfa5, #eee4d1 1.4%, #faf5e9 5%, #fffbf1 28%, #fffaf0 97%, #efe5d2);
  box-shadow: 1px 2px #b5ab95, 2px 3px #f7efdb, 3px 4px #b8af9a, 4px 5px #f5edda;
}
.index-page::before {
  content: ''; position: absolute; right: 33px; top: -5px; width: 16px; height: 26px;
  background: linear-gradient(90deg, #99503a, #b3674d 32%, #a45b43);
  clip-path: polygon(0 0,100% 0,100% 100%,50% 75%,0 100%);
}
.library-toolbar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; padding-bottom: 17px; border-bottom: 3px double #aa9a77; }
.library-toolbar h2 { font: 700 clamp(24px, 2.2vw, 32px)/1.3 var(--book-serif); letter-spacing: .05em; }
.create-button, .new-actions .workspace-button--primary { --workspace-accent: #28564c; --workspace-accent-hover: #193e35; border-radius: 6px; font-size: 13px; }
.library-state { padding: 22px 0; border: 0; box-shadow: none; background: transparent; color: var(--book-muted); }
.library-error, .library-notice { margin-top: 14px; }
.library-error { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
.ledger-card { position: relative; border-bottom: 1px solid var(--book-line); }
.ledger-open {
  width: 100%; display: grid; gap: 13px; min-width: 0; padding: 19px 4px 16px;
  border: none; border-radius: 4px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.ledger-open:hover:not(:disabled) { background: #ad976610; }
.ledger-open:disabled { cursor: wait; }
.ledger-heading { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px; }
.ledger-cover {
  position: relative; display: grid; place-items: center; width: 40px; height: 50px; padding-left: 4px;
  border-radius: 2px 5px 5px 2px; background: linear-gradient(90deg, #713e31, #975741 12%, #bb8164 15%, #995e48 20%, #ac7158);
  box-shadow: 1px 2px #d6c9af, 1px 3px #977e60; color: #fff2ce; font: 600 19px var(--book-serif);
}
.ledger-cover::before { content: ''; position: absolute; inset: 6px 5px 6px 8px; border-block: 1px solid #f1d79b94; }
.ledger-card--active .ledger-cover { background: linear-gradient(90deg, #1a3832, #326453 12%, #74937c 15%, #305948 20%, #3a6e5c); }
.ledger-name b, .ledger-name small { display: block; overflow-wrap: anywhere; }
.ledger-name b { font: 700 20px/1.4 var(--book-serif); }
.ledger-name small, .ledger-stats small { color: var(--book-muted); font-size: 12px; }
.ledger-name small { margin-top: 3px; font-variant-numeric: tabular-nums; }
.active-pill { align-self: start; border: 1px solid #8aa891; color: #35634e; padding: 3px 6px; border-radius: 3px; font-size: 11px; white-space: nowrap; }
.ledger-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.ledger-stats > span { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 6px; padding-right: 5px; }
.ledger-stats > span + span { padding-left: 12px; border-left: 1px solid var(--book-line); }
.ledger-stats b { font: 600 18px/1.2 Georgia, serif; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.ledger-foot { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px; }
.ledger-state, .ledger-delete { font-size: 12px; color: var(--book-muted); }
.ledger-state::before { content: ''; display: inline-block; width: 6px; height: 6px; margin: 0 6px 2px 0; border-radius: 50%; background: currentColor; }
.ledger-state--ready { color: #396452; }
.ledger-state--warn { color: #896021; }
.ledger-enter { font-size: 13px; font-weight: 700; }
.ledger-card:has(.ledger-delete) .ledger-foot { padding-right: 60px; }
.ledger-actions { position: absolute; right: 0; bottom: 9px; }
.ledger-delete { min-height: 32px; padding: 5px 7px; background: transparent; border: 0; color: #86554a; font-family: inherit; cursor: pointer; }
.ledger-delete:hover:not(:disabled) { text-decoration: underline; }
.new-ledger { display: grid; gap: 12px; margin-top: 18px; padding: 16px; border: 1px solid var(--book-line); border-radius: 6px; background: #faf5e9; box-shadow: none; }
.new-ledger h2 { font-size: 16px; }
.new-ledger h2 + p { margin-top: 4px; color: var(--book-muted); }
.new-fields { display: grid; gap: 8px; }
.new-fields label { font-weight: 650; }
.new-actions, .delete-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.delete-ledger {
  margin: auto;
  width: min(440px, calc(100% - 32px));
  max-height: calc(100% - 32px);
  padding: clamp(16px, 5vw, 24px);
  color: var(--workspace-ink);
  overflow-wrap: anywhere;
}
.delete-ledger::backdrop { background: rgb(15 31 48 / .48); }
.delete-ledger h2 { font-size: 19px; }
.delete-ledger p { margin-top: 9px; color: var(--workspace-muted); }
.delete-ledger p[role='alert'] { color: var(--workspace-danger); }
.delete-actions { justify-content: flex-end; margin-top: 18px; }

@media (max-width: 760px) {
  .book-spread { grid-template-columns: minmax(0, 1fr); }
  .title-page { min-height: 0; gap: 16px; padding: 27px 24px 23px; }
  .cover-center h1 { font-size: 34px; }
  .compass { width: 46px; margin-bottom: 11px; }
  .cover-center p { max-width: 32em; margin-top: 8px; line-height: 1.65; font-size: 13px; }
  .desktop-break { display: none; }
  .library-summary { padding-top: 11px; }
  .title-page::before { inset: 8px; }
  .title-page::after { inset: 11px; }
  .index-page { padding: 28px clamp(16px, 5vw, 30px) 25px; border-radius: 1px 1px 9px 3px; }
  .index-page::before { top: 0; right: 22px; height: 17px; width: 12px; }
}
@media (max-width: 410px) {
  .ledger-heading { gap: 8px; }
  .ledger-stats > span { flex-direction: column; }
  .ledger-stats > span + span { padding-left: 8px; }
}
</style>
