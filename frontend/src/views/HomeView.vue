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
    <header class="workspace-heading library-heading">
      <div class="workspace-title">
        <p class="workspace-kicker">账本管理</p>
        <h1>选择一本账本</h1>
        <p class="workspace-lede">每本账本独立保存流水。打开账本后，就能查看报告、整理分类或导入新账单。</p>
      </div>
      <button
        ref="createButton"
        class="workspace-button workspace-button--primary"
        type="button"
        :aria-expanded="createOpen"
        :disabled="busy"
        @click="createOpen ? closeCreate() : openCreate()"
      >
        <span aria-hidden="true">＋</span> 新建账本
      </button>
    </header>

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

    <section aria-labelledby="library-heading" :aria-busy="loading">
      <div class="library-toolbar">
        <h2 id="library-heading">全部账本</h2>
        <ul v-if="libraries.length" class="library-summary" aria-label="账本库概况">
          <li v-for="item in summary" :key="item.label"><b>{{ number.format(item.value) }}</b> {{ item.label }}</li>
        </ul>
      </div>

      <p v-if="loading && !libraries.length" class="workspace-panel library-state" role="status">正在加载账本…</p>
      <p v-else-if="!libraries.length && !error" class="workspace-panel library-state">还没有账本。先新建一本，再导入第一份账单。</p>
      <div v-else-if="libraries.length" class="ledger-grid">
        <article
          v-for="item in libraries"
          :key="item.id"
          class="workspace-panel ledger-card"
          :class="{ 'ledger-card--active': item.active }"
        >
          <button type="button" class="ledger-open" :disabled="busy" @click="enter(item)">
            <span class="ledger-heading">
              <span class="ledger-cover" aria-hidden="true">{{ Array.from(item.name)[0] }}</span>
              <span class="ledger-name">
                <b>{{ item.name }}</b>
                <small>{{ rangeLabel(item) }}</small>
              </span>
              <span v-if="item.active" class="workspace-badge workspace-badge--ready active-pill">当前账本</span>
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
.library-heading { padding: 4px 0 24px; border-bottom: 1px solid var(--workspace-line); }
.library-heading h1 { font-size: clamp(31px, 5vw, 49px); }
.library-toolbar { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 10px 16px; margin-bottom: 14px; }
.library-toolbar h2, .new-ledger h2 { font-size: 18px; }
.library-summary { display: flex; flex-wrap: wrap; gap: 6px 16px; list-style: none; color: var(--workspace-muted); font-size: 13px; }
.library-summary b { color: var(--workspace-ink); font-variant-numeric: tabular-nums; }
.library-state { padding: 24px; color: var(--workspace-muted); }
.library-error { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.library-notice { margin-bottom: 20px; }
.ledger-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 16px; }
.ledger-card { display: flex; flex-direction: column; }
.ledger-card:has(.ledger-open:hover:not(:disabled)) { border-color: var(--workspace-line-accent); background: var(--workspace-soft); }
.ledger-card--active { border-color: var(--workspace-line-accent); }
.ledger-open {
  display: flex;
  flex: 1;
  flex-direction: column;
  width: 100%;
  min-width: 0;
  padding: 19px;
  border: 0;
  border-radius: inherit;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.ledger-open:disabled { cursor: wait; }
.ledger-actions { display: flex; justify-content: flex-end; padding: 0 13px 9px; }
.ledger-delete {
  min-height: 36px;
  padding: 6px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--workspace-danger);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.ledger-delete:hover:not(:disabled) { background: #fff0ee; }
.ledger-delete:disabled { opacity: .58; cursor: not-allowed; }
.ledger-heading { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: 12px; width: 100%; }
.ledger-cover {
  display: grid;
  place-items: center;
  width: 44px;
  height: 52px;
  border-radius: 5px 9px 9px 5px;
  background: linear-gradient(90deg, var(--workspace-accent) 4px, var(--workspace-tint) 4px);
  color: var(--workspace-accent);
  font-size: 20px;
  font-weight: 750;
}
.ledger-card--active .ledger-cover {
  background: linear-gradient(90deg, var(--workspace-accent-hover) 4px, var(--workspace-accent) 4px);
  color: #fff;
}
.ledger-name b, .ledger-name small { display: block; overflow-wrap: anywhere; }
.ledger-name b { font-size: 17px; line-height: 1.35; }
.ledger-name small { margin-top: 5px; color: var(--workspace-muted); font-size: 12px; line-height: 1.5; }
.active-pill { white-space: nowrap; }
.ledger-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; width: 100%; margin: 16px 0; }
.ledger-stats > span { padding: 9px; border-radius: 9px; background: var(--workspace-soft); overflow-wrap: anywhere; }
.ledger-stats b, .ledger-stats small { display: block; }
.ledger-stats b { font-size: 17px; font-variant-numeric: tabular-nums; }
.ledger-stats small { margin-top: 2px; color: var(--workspace-muted); font-size: 12px; }
.ledger-foot { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; width: 100%; margin-top: auto; }
.ledger-state { color: var(--workspace-muted); font-size: 12px; }
.ledger-state::before { content: ''; display: inline-block; width: 6px; height: 6px; margin: 0 6px 2px 0; border-radius: 50%; background: currentColor; }
.ledger-state--ready, .ledger-enter { color: var(--workspace-accent); }
.ledger-state--warn { color: var(--workspace-warning); }
.ledger-enter { font-weight: 750; }
.new-ledger { display: grid; grid-template-columns: minmax(0, .7fr) minmax(0, 1fr); gap: 16px 24px; margin-bottom: 24px; padding: 22px; }
.new-ledger h2 + p { margin-top: 6px; color: var(--workspace-muted); }
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

@media (max-width: 560px) {
  .library-heading { padding-bottom: 18px; }
  .ledger-open { padding: 15px; }
  .new-ledger { grid-template-columns: minmax(0, 1fr); padding: 16px; }
}
@media (max-width: 360px) {
  .ledger-heading { grid-template-columns: auto minmax(0, 1fr); }
  .active-pill { grid-column: 2; }
}
</style>
