<script setup lang="ts">
/**
 * App.vue —— 全局壳子
 *
 * 首页是账本库；四个业务页属于「当前账本」。左上角始终显示当前账本名，
 * 点开可以回首页或切换到另一本账本。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import FloatingLedgerAssistant from '@/components/FloatingLedgerAssistant.vue'
import { useLibraries } from '@/composables/useLibraries'
import type { LedgerLibrary } from '@/api/libraries'
import '@/styles/atlas.css'
import '@/styles/workspace.css'

const route = useRoute()
const router = useRouter()
const { libraries, active, refresh, choose } = useLibraries()

const menuOpen = ref(false)
const menuBusy = ref('')
const menuError = ref('')
const brandButton = ref<HTMLButtonElement | null>(null)
const menuPanel = ref<HTMLElement | null>(null)
const brandWrap = ref<HTMLElement | null>(null)

const workspaceRoutes = new Set(['home', 'transactions', 'ai-settings', 'ai-history'])
const isHome = computed(() => route.name === 'home')
const isWorkspace = computed(() => typeof route.name === 'string' && workspaceRoutes.has(route.name))
const brandName = computed(() => active.value?.name ?? '我的账本')

function closeMenu(restoreFocus = false) {
  if (!menuOpen.value) return
  menuOpen.value = false
  menuError.value = ''
  if (restoreFocus) brandButton.value?.focus({ preventScroll: true })
}
async function toggleMenu() {
  if (menuOpen.value) {
    closeMenu()
    return
  }
  menuOpen.value = true
  await nextTick()
  menuPanel.value?.querySelector<HTMLElement>('[role="menuitem"]')?.focus({ preventScroll: true })
}
function onOutside(event: Event) {
  if (event.target instanceof Node && !brandWrap.value?.contains(event.target)) closeMenu()
}
function onEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || event.isComposing) return
  if (menuOpen.value) {
    event.preventDefault()
    closeMenu(true)
  }
}

async function goHome() {
  closeMenu()
  await router.push({ name: 'home' })
}
async function pick(item: LedgerLibrary) {
  if (menuBusy.value) return
  menuBusy.value = item.id
  menuError.value = ''
  try {
    await choose(item.id)
    closeMenu()
    await router.push({ name: 'bill' })
  } catch (reason) {
    menuError.value = reason instanceof Error ? reason.message : '无法切换账本'
  } finally {
    menuBusy.value = ''
  }
}
function onMenuKeys(event: KeyboardEvent) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  const items = Array.from(menuPanel.value?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
  if (!items.length) return
  event.preventDefault()
  const index = items.findIndex(item => item === document.activeElement)
  const offset = event.key === 'ArrowDown' ? 1 : -1
  items[(index + offset + items.length) % items.length]?.focus()
}

onMounted(() => {
  refresh().catch(() => {})
  window.addEventListener('pointerdown', onOutside, true)
  window.addEventListener('focusin', onOutside)
  window.addEventListener('keydown', onEscape)
})
onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', onOutside, true)
  window.removeEventListener('focusin', onOutside)
  window.removeEventListener('keydown', onEscape)
})
</script>

<template>
  <div class="app" :class="{ workspace: isWorkspace }">
    <header class="app-header" :class="{ 'app-header--library': isHome }">
      <div ref="brandWrap" class="brand-wrap">
        <button
          ref="brandButton"
          type="button"
          class="brand"
          aria-haspopup="menu"
          :aria-expanded="menuOpen"
          data-testid="ledger-switcher"
          @click="toggleMenu"
        >
          <span class="brand-mark" aria-hidden="true">A</span>
          <span class="brand-text">
            <b>{{ brandName }}</b>
            <small>{{ isHome ? '账本库 · 点击切换账本' : '当前账本 · 点击切换' }}</small>
          </span>
          <svg class="brand-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m4.5 6.5 3.5 3.5 3.5-3.5" />
          </svg>
        </button>

        <div
          v-if="menuOpen"
          ref="menuPanel"
          class="brand-menu"
          role="menu"
          aria-label="切换账本"
          @keydown="onMenuKeys"
        >
          <button type="button" role="menuitem" class="menu-home" @click="goHome">
            <span class="menu-icon" aria-hidden="true">⌂</span>
            <span><b>首页</b><small>账本库 · 选择或新建账本</small></span>
          </button>
          <p class="menu-label">全部账本</p>
          <button
            v-for="item in libraries"
            :key="item.id"
            type="button"
            role="menuitem"
            class="menu-ledger"
            :class="{ active: item.active }"
            :disabled="!!menuBusy"
            @click="pick(item)"
          >
            <span class="menu-dot" aria-hidden="true" />
            <span class="menu-ledger-text">
              <b>{{ item.name }}</b>
              <small>
                {{ item.stats.transactions.toLocaleString('zh-CN') }} 笔流水
                <template v-if="item.stats.uninitialized"> · {{ item.stats.uninitialized }} 笔待整理</template>
              </small>
            </span>
            <span v-if="menuBusy === item.id" class="menu-busy">切换中…</span>
            <span v-else-if="item.active" class="menu-check" aria-label="当前账本">✓</span>
          </button>
          <p v-if="!libraries.length" class="menu-empty">还没有账本，请回首页新建。</p>
          <p v-if="menuError" class="menu-error" role="alert">{{ menuError }}</p>
        </div>
      </div>

      <nav v-if="!isHome" class="nav">
        <RouterLink to="/bill" class="nav-item">账单报告</RouterLink>
        <RouterLink to="/v2" class="nav-item">∑ 账本代数 V2</RouterLink>
        <RouterLink to="/v2/assistant" class="nav-item">✎ ATLAS</RouterLink>
        <RouterLink to="/import" class="nav-item">导入账单</RouterLink>
        <RouterLink to="/ai/settings" class="nav-item">AI 设置</RouterLink>
      </nav>
      <span v-else class="home-corner">{{ libraries.length ? `${libraries.length} 本账本` : '账本库' }}</span>
    </header>

    <main class="app-main" :class="{ 'app-main--wide': route.name === 'v2' || route.name === 'agentcore' }">
      <RouterView />
    </main>
    <FloatingLedgerAssistant v-if="route.name !== 'agentcore' && route.name !== 'home'" />
  </div>
</template>

<style scoped>
.app-header {
  position: relative;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 32px;
  min-height: 70px;
  padding: 10px max(24px, calc((100vw - 1128px) / 2));
  background: rgba(255, 255, 255, .92);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--color-border);
}
.brand-wrap { position: relative; flex: none; min-width: 0; }
.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  max-width: min(390px, 46vw);
  padding: 5px 8px 5px 5px;
  border: 1px solid transparent;
  border-radius: 14px;
  background: transparent;
  color: var(--color-text);
  text-align: left;
  font: inherit;
  cursor: pointer;
}
.brand:hover, .brand[aria-expanded="true"] { background: #f4f7fb; border-color: #e7edf5; }
.brand-mark {
  flex: none;
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 13px;
  background: linear-gradient(145deg, #376fae, #77a8df);
  color: #fff;
  font-size: 19px;
  font-weight: 800;
  box-shadow: 0 8px 18px rgba(55, 111, 174, .22);
}
.brand-text { min-width: 0; }
.brand-text b, .brand-text small { display: block; }
.brand-text b {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 15px;
}
.brand-text small { margin-top: 2px; color: var(--color-muted); font-size: 11px; white-space: nowrap; }
.brand-chevron {
  flex: none;
  width: 16px;
  height: 16px;
  color: var(--workspace-accent, #5278a8);
  transition: transform .18s ease;
}
.brand[aria-expanded="true"] .brand-chevron { transform: rotate(180deg); }

.brand-menu {
  position: absolute;
  z-index: 70;
  top: calc(100% + 7px);
  left: 0;
  width: 310px;
  max-width: calc(100vw - 32px);
  max-height: min(430px, calc(100vh - 100px));
  overflow: auto;
  padding: 7px;
  border: 1px solid #dde6f0;
  border-radius: 16px;
  background: rgba(255, 255, 255, .99);
  box-shadow: 0 8px 20px rgba(38, 56, 82, .08), 0 24px 60px rgba(38, 56, 82, .18);
}
.brand-menu button {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--color-text);
  text-align: left;
  font: inherit;
  cursor: pointer;
}
.brand-menu button:hover, .brand-menu button:focus-visible { background: #f3f7fc; }
.brand-menu button.active { background: #eaf2fb; }
.menu-home { border-bottom: 1px solid #eef2f7; border-radius: 10px 10px 4px 4px !important; margin-bottom: 4px; }
.menu-icon {
  flex: none;
  width: 31px;
  height: 31px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: #eaf2fb;
  color: #3d72ae;
  font-size: 16px;
}
.brand-menu b, .brand-menu small { display: block; }
.brand-menu b { font-size: 13px; }
.brand-menu small { margin-top: 3px; color: var(--color-muted); font-size: 10px; }
.menu-label { padding: 8px 10px 4px; color: #919cab; font-size: 10px; font-weight: 700; letter-spacing: .08em; }
.menu-ledger-text { min-width: 0; flex: 1; }
.menu-ledger-text b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.menu-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: #b7c2cf; }
.menu-ledger.active .menu-dot { background: var(--workspace-accent, #376fae); box-shadow: 0 0 0 3px #376fae1f; }
.menu-check { flex: none; color: var(--workspace-accent, #376fae); font-size: 14px; font-weight: 800; }
.menu-busy { flex: none; color: #7b8898; font-size: 10px; }
.menu-empty { padding: 10px; color: var(--color-muted); font-size: 12px; }
.menu-error { margin: 4px; padding: 8px 10px; border-radius: 8px; background: #fff4f4; color: #a04449; font-size: 11px; }

.nav { display: flex; gap: 8px; }
.nav-item {
  color: var(--color-muted);
  text-decoration: none;
  padding: 9px 13px;
  border-radius: 10px;
}
.nav-item.router-link-active {
  color: var(--color-text);
  font-weight: 600;
  background: #f0f4f9;
}
.home-corner { color: #9aa5b4; font-size: 11px; letter-spacing: .08em; }
.app-main { max-width: 1128px; margin: 0 auto; padding: 24px; }
.app-main.app-main--wide { max-width: 100%; padding: 18px clamp(12px, 2.2vw, 36px) 34px; }

@media (max-width: 860px) {
  .app-header { flex-wrap: wrap; gap: 8px 20px; }
  .nav { min-width: 0; max-width: 100%; overflow-x: auto; padding-bottom: 2px; scrollbar-width: thin; }
  .nav-item { white-space: nowrap; }
}
@media (max-width: 720px) {
  .app-header { min-height: 58px; padding: 8px 16px; }
  .brand { max-width: 60vw; }
  .brand small { display: none; }
  .brand-mark { width: 34px; height: 34px; border-radius: 11px; }
  .brand-text b { font-size: 14px; }
  .nav-item { padding: 8px 9px; font-size: 12px; }
  .app-main { padding: 16px; }
}
@media (prefers-reduced-motion: reduce) {
  .brand-chevron { transition: none; }
}

.workspace .app-header {
  min-height: 66px;
  gap: 10px 24px;
  padding: 9px max(var(--workspace-gutter), calc((100% - var(--workspace-width)) / 2));
  background: var(--workspace-paper);
  border-color: var(--workspace-line);
  backdrop-filter: none;
}
.workspace .brand-wrap { flex: 0 1 auto; }
.workspace .brand {
  max-width: min(340px, 62vw);
  padding-left: 0;
  color: var(--workspace-ink);
}
.workspace .app-header--library .brand { max-width: min(380px, 66vw); }
.workspace .brand:hover, .workspace .brand[aria-expanded="true"] { background: var(--workspace-soft); border-color: transparent; }
.workspace .brand-mark { border-radius: 10px; background: var(--workspace-accent); box-shadow: none; }
.workspace .brand-text small { overflow: hidden; color: var(--workspace-muted); text-overflow: ellipsis; }
.workspace .brand-menu {
  max-width: calc(100vw - 2 * var(--workspace-gutter));
  border-color: var(--workspace-line);
  background: var(--workspace-paper);
  box-shadow: 0 12px 32px #20303e20;
}
.workspace .brand-menu button { color: var(--workspace-ink); }
.workspace .brand-menu button:hover, .workspace .brand-menu button:focus-visible,
.workspace .brand-menu button.active, .workspace .menu-icon { background: var(--workspace-tint); }
.workspace .brand-menu small, .workspace .menu-label { color: var(--workspace-muted); }
.workspace .menu-icon, .workspace .menu-check { color: var(--workspace-accent); }
.workspace .menu-home { border-color: var(--workspace-line); }
.workspace .home-corner { flex: none; color: var(--workspace-muted); font-size: 12px; letter-spacing: 0; }
.workspace .nav { flex-wrap: wrap; gap: 4px; overflow: visible; padding: 0; }
.workspace .nav-item { color: var(--workspace-muted); white-space: nowrap; }
.workspace .nav-item:hover, .workspace .nav-item.router-link-active { background: var(--workspace-tint); color: var(--workspace-accent); }
.workspace .app-main {
  width: min(var(--workspace-width), calc(100% - 2 * var(--workspace-gutter)));
  max-width: none;
  padding: clamp(22px, 4vw, 48px) 0 54px;
}
@media (max-width: 720px) {
  .workspace .app-header { min-height: 58px; }
  .workspace .nav { flex-basis: 100%; }
  .workspace .nav-item { padding: 8px; }
  .workspace .app-header:not(.app-header--library) + .app-main { padding-bottom: 112px; }
}
</style>
