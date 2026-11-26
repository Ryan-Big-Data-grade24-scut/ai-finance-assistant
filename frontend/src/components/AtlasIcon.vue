<script setup lang="ts">
const paths = {
  atlas: 'M12 2.9c.9 5.3 3.8 8.2 9.1 9.1-5.3.9-8.2 3.8-9.1 9.1-.9-5.3-3.8-8.2-9.1-9.1 5.3-.9 8.2-3.8 9.1-9.1Z M18.5 3.5v4m-2-2h4',
  plus: 'M12 5v14M5 12h14',
  chat: 'M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9l-5 3v-3a2 2 0 0 1-1-2V7a2 2 0 0 1 2-2Z',
  ledger: 'M6 3h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm2 0v18m4-13h4m-4 4h4',
  file: 'M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10Zm0 0v7h7M8 14h8m-8 3h5',
  list: 'M9 6h12M9 12h12M9 18h12M3 6l1 1 2-2m-3 7l1 1 2-2m-3 7l1 1 2-2',
  calendar: 'M7 3v4m10-4v4M4 10h16M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm3 9h2m4 0h2m-8 3h2',
  history: 'M3 5v5h5m-4-1a9 9 0 1 1 .3 8m7.7-9v5l3 2',
  remove: 'M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5',
  expand: 'M13 4h7v7m0-7-9 9m7 1v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  close: 'M6 6l12 12M18 6 6 18',
  arrow: 'M4 12h15m-6-6 6 6-6 6',
  send: 'M12 20V4M5 11l7-7 7 7',
  stop: 'M6 6h12v12H6z',
  chevron: 'm6 9 6 6 6-6',
  palette: 'M12 3a9 9 0 1 0 0 18h1.5a2.4 2.4 0 0 0 1.6-4.1 1.2 1.2 0 0 1 .8-2.1H18a3 3 0 0 0 3-3.2A9 9 0 0 0 12 3ZM7.4 11.2h.1m2.2-3.4h.1m3.9-.3h.1m3.1 2.4h.1',
  check: 'm5 12 4 4L19 6',
  minus: 'M5 12h14',
}
defineProps<{ name: keyof typeof paths }>()
</script>

<template>
  <svg class="atlas-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path :d="paths[name]" />
  </svg>
</template>

<style scoped>
.atlas-icon { display: block; width: 1em; height: 1em; flex: none; }
</style>
