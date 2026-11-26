<template>
  <span class="session-spinner" role="img" aria-label="正在运行" title="正在运行" />
</template>

<style scoped>
.session-spinner {
  display: inline-block; flex: none; width: 14px; height: 14px;
  border: 2px solid var(--atlas-line); border-top-color: var(--atlas-accent); border-radius: 50%;
  animation: session-spin .8s linear infinite;
}
@keyframes session-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .session-spinner { animation: none; border-color: var(--atlas-accent); border-right-color: var(--atlas-line); }
}
</style>
