<script setup lang="ts">
import maleSprite from '@/assets/atlas/male.webp'
import femaleSprite from '@/assets/atlas/female.webp'

withDefaults(defineProps<{
  variant?: number
  state?: 'idle' | 'thinking' | 'open'
}>(), {
  variant: 0,
  state: 'idle',
})
</script>

<template>
  <span class="atlas-mascot" :class="`is-${state}`" aria-hidden="true">
    <img class="mascot-character" :src="variant === 1 ? femaleSprite : maleSprite" alt="" draggable="false" />
    <span v-if="state === 'thinking'" class="mascot-signal"><i></i><i></i><i></i></span>
  </span>
</template>

<style scoped>
.atlas-mascot { position: relative; display: block; width: 100%; height: 100%; }
.mascot-character { display: block; width: 100%; height: 100%; object-fit: contain; pointer-events: none; }
.is-open .mascot-character { transform-origin: center bottom; animation: atlas-greet .36s ease-out both; }
.mascot-signal { position: absolute; top: 0; right: 0; display: flex; align-items: center; gap: 2px; }
.mascot-signal i {
  width: clamp(3px, .65em, 5px); aspect-ratio: 1; border-radius: 50%;
  background: #24a58d; box-shadow: 0 0 0 1px #f1fff6;
  animation: atlas-signal 1.35s ease-in-out infinite;
}
.mascot-signal i:nth-child(2) { animation-delay: .15s; }
.mascot-signal i:nth-child(3) { animation-delay: .3s; }
@keyframes atlas-greet { 40% { transform: translateY(-4px) rotate(-4deg); } }
@keyframes atlas-signal { 50% { transform: translateY(-3px); } }
@media (prefers-reduced-motion: reduce) {
  .is-open .mascot-character, .mascot-signal i { animation: none; }
}
</style>
