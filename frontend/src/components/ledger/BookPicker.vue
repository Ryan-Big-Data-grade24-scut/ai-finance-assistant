<script setup lang="ts">
/**
 * BookPicker.vue —— 选账本 / 建账本
 *
 * 账本 = 用户定义的一个「阶段」，每阶段一本独立账，各有自己的树。
 * 建账时按日期区间**冻结**成员集 —— 之后区间不可改。
 */
import { ref } from 'vue'
import type { LedgerBook } from '@/types/ledger'

defineProps<{
  books: LedgerBook[]
  currentBookId: string
  pending: boolean
}>()

const emit = defineEmits<{
  (e: 'select', bookId: string): void
  (e: 'create', payload: { name: string; dateFrom: string; dateTo: string }): void
}>()

const showForm = ref(false)
const name = ref('')
const dateFrom = ref('')
const dateTo = ref('')

function submit() {
  if (!name.value.trim() || !dateFrom.value || !dateTo.value) return
  emit('create', { name: name.value, dateFrom: dateFrom.value, dateTo: dateTo.value })
  showForm.value = false
  name.value = ''
}
</script>

<template>
  <div class="picker">
    <select
      :value="currentBookId"
      :disabled="pending"
      @change="emit('select', ($event.target as HTMLSelectElement).value)"
    >
      <option v-for="b in books" :key="b.book_id" :value="b.book_id">
        {{ b.name }}（{{ b.date_from }} ~ {{ b.date_to }}，{{ b.member_count }} 笔）
      </option>
      <option v-if="!books.length" value="">还没有账本</option>
    </select>
    <button class="ghost" @click="showForm = !showForm">
      {{ showForm ? '取消' : '建一本账' }}
    </button>

    <form v-if="showForm" class="form" @submit.prevent="submit">
      <input v-model="name" placeholder="账本名字，如 2026 十月" />
      <input v-model="dateFrom" type="date" />
      <input v-model="dateTo" type="date" />
      <button type="submit">建账</button>
      <small>区间在建账时冻结，之后不可改。</small>
    </form>
  </div>
</template>

<style scoped>
.picker { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
select, input {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 6px 9px;
  font-size: 13px;
  background: #fff;
  color: var(--color-text);
}
button {
  border: 1px solid var(--color-border);
  background: #fff;
  border-radius: 8px;
  padding: 6px 11px;
  font-size: 13px;
  cursor: pointer;
}
button.ghost { color: var(--color-muted); }
.form {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  width: 100%;
  margin-top: 6px;
}
.form small { color: var(--color-muted); font-size: 11px; }
</style>
