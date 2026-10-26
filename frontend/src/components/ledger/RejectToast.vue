<script setup lang="ts">
/**
 * RejectToast.vue —— 引擎拒绝时的提示
 *
 * ⚠️ 文案**原样来自后端**（LedgerError.message）。
 *    前端不自己拼拒绝文案 —— 两端口径必须一致。
 */
defineProps<{
  message: string
  visible: boolean
}>()

const emit = defineEmits<{ (e: 'close'): void }>()
</script>

<template>
  <Transition name="toast">
    <div v-if="visible && message" class="toast" @click="emit('close')">
      <span class="icon">!</span>
      <span class="text">{{ message }}</span>
    </div>
  </Transition>
</template>

<style scoped>
.toast {
  position: fixed;
  left: 50%;
  bottom: 28px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 9px;
  background: #2b3440;
  color: #fff;
  padding: 10px 15px;
  border-radius: 10px;
  font-size: 13px;
  box-shadow: 0 12px 32px rgba(20, 28, 38, .3);
  cursor: pointer;
  max-width: 80vw;
}
.icon {
  width: 18px; height: 18px;
  display: grid; place-items: center;
  border-radius: 50%;
  background: #e08a2e;
  font-weight: 700;
  font-size: 12px;
}
.toast-enter-active, .toast-leave-active { transition: opacity .18s ease, transform .18s ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translate(-50%, 8px); }
</style>
