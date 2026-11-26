<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { deleteAIPrivacy, getAIPrivacy, saveAIPrivacy, type AIPrivacyEntry } from '@/api/ai'

const entries = ref<AIPrivacyEntry[]>([])
const drafts = ref<Record<string, string>>(Object.create(null))
const original = ref('')
const alias = ref('')
const busy = ref(false)
const loaded = ref(false)
const notice = ref('')
const error = ref('')

function showEntries(next: AIPrivacyEntry[]) {
  entries.value = next
  const updated = Object.create(null) as Record<string, string>
  for (const entry of next) {
    updated[entry.original] = drafts.value[entry.original] ?? entry.alias
  }
  drafts.value = updated
  loaded.value = true
}

async function refresh() {
  busy.value = true
  notice.value = ''
  error.value = ''
  try {
    showEntries((await getAIPrivacy()).entries)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '读取隐私设置失败。'
  } finally {
    busy.value = false
  }
}

async function save(name: string, replacement: string | null, clearForm = false) {
  busy.value = true
  notice.value = ''
  error.value = ''
  try {
    const result = await saveAIPrivacy(name.trim(), replacement)
    delete drafts.value[name.trim()]
    showEntries(result.entries)
    if (clearForm) {
      original.value = ''
      alias.value = ''
    }
    notice.value = replacement === '' ? '已设置为允许 AI 看到原文。'
      : replacement === null ? '已恢复自动筛查。' : '别名已保存，下次请求即生效。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存隐私设置失败。'
  } finally {
    busy.value = false
  }
}

async function remove(name: string) {
  busy.value = true
  notice.value = ''
  error.value = ''
  try {
    showEntries((await deleteAIPrivacy(name)).entries)
    notice.value = '筛查项已删除。重新筛查会跳过该项，可手动添加回来。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除筛查项失败。'
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <section class="workspace-panel privacy-card" aria-labelledby="privacy-title">
    <div class="privacy-heading">
      <h2 id="privacy-title">隐私设置</h2>
      <button type="button" class="workspace-button" :disabled="busy" @click="refresh">
        {{ !loaded && busy ? '筛查中…' : '重新筛查' }}
      </button>
    </div>
    <p class="privacy-help">疑似中文姓名、昵称、单独姓氏和英文名字会自动使用别名。删除误判项会移除别名，重新筛查也会跳过该项。星图、账单明细和报告仍显示原文。</p>

    <div v-if="entries.length" class="privacy-list" role="list" aria-label="隐私别名">
      <div v-for="entry in entries" :key="entry.original" class="privacy-row" role="listitem">
        <div class="privacy-source">
          <strong>{{ entry.original }}</strong>
          <small>{{ entry.mode === 'keep' ? '允许原文' : entry.mode === 'auto' ? '自动保护' : '自定义别名' }}{{ entry.count ? ' · ' + entry.count + ' 笔交易' : '' }}</small>
        </div>
        <form class="privacy-edit" @submit.prevent="save(entry.original, drafts[entry.original]!.trim())">
          <input v-model="drafts[entry.original]" class="workspace-field" :aria-label="entry.original + ' 的别名'"
                 placeholder="例如 同学A" maxlength="120" :disabled="busy" required />
          <button type="submit" class="workspace-button" :disabled="busy || !drafts[entry.original]?.trim() || drafts[entry.original]?.trim() === entry.alias">保存</button>
          <button v-if="entry.mode !== 'keep'" type="button" class="workspace-button"
                  :disabled="busy" @click="save(entry.original, '')">保留原文</button>
          <button v-else-if="entry.detected" type="button" class="workspace-button" :disabled="busy"
                  @click="save(entry.original, null)">恢复自动</button>
          <button type="button" class="workspace-button workspace-button--danger" :disabled="busy"
                  :aria-label="'删除筛查项 ' + entry.original" @click="remove(entry.original)">删除</button>
        </form>
      </div>
    </div>
    <p v-else-if="loaded" class="privacy-empty">当前账本没有筛查出的姓名或已保存的别名。</p>

    <form class="privacy-custom" @submit.prevent="save(original, alias.trim(), true)">
      <h3>添加自定义别名</h3>
      <p class="privacy-help">可添加筛查遗漏的姓名、昵称、地址或备注中的一段文字。</p>
      <div class="privacy-fields">
        <label>隐私原文<input v-model="original" class="workspace-field" placeholder="输入要隐藏的文字" maxlength="240" :disabled="busy" required /></label>
        <label>AI 可见别名<input v-model="alias" class="workspace-field" placeholder="例如 同学A" maxlength="120" :disabled="busy" required /></label>
        <button type="submit" class="workspace-button workspace-button--primary" :disabled="busy || !original.trim() || !alias.trim()">添加别名</button>
      </div>
    </form>
    <p v-if="notice" class="workspace-note privacy-feedback" role="status">{{ notice }}</p>
    <p v-if="error" class="workspace-note workspace-note--error privacy-feedback" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.privacy-card { padding: clamp(16px, 2.5vw, 26px); }
h2 { font-size: 18px; }
h3 { font-size: 14px; }
.privacy-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.privacy-help, small { color: var(--workspace-muted); font-size: 12px; line-height: 1.65; }
.privacy-help { margin-top: 5px; }
.privacy-list { max-height: 320px; overflow: auto; margin-top: 14px; border-radius: 10px; background: var(--workspace-soft); }
.privacy-row { display: grid; grid-template-columns: minmax(110px, .75fr) minmax(0, 1.7fr); align-items: center; gap: 12px; padding: 10px 12px; }
.privacy-row + .privacy-row { border-top: 1px solid var(--workspace-line, #e2e8f0); }
.privacy-source { min-width: 0; overflow-wrap: anywhere; }
.privacy-source small { display: block; }
.privacy-edit { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.privacy-edit input { flex: 1 1 112px; width: auto; }
.privacy-edit .workspace-button { padding-inline: 8px; }
.privacy-empty { color: var(--workspace-muted); margin-top: 12px; }
.privacy-custom { margin-top: 16px; }
.privacy-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; align-items: end; gap: 8px; margin-top: 9px; }
.privacy-fields label { font-size: 12px; font-weight: 650; }
.privacy-fields input { margin-top: 5px; }
.privacy-feedback { margin-top: 12px; }
@media (max-width: 620px) {
  .privacy-row, .privacy-fields { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .privacy-fields .workspace-button { justify-self: start; }
}
</style>
