<script setup lang="ts">
/**
 * App.vue —— 只是路由出口，不是主页
 *
 * 全局壳子（导航栏）放这里，三个页面共享的部分放这里。
 * 具体业务内容全在 views/ 里。
 */
</script>

<template>
  <div class="app">
    <header class="app-header">
      <RouterLink to="/bill" class="brand">
        <span class="brand-mark">F</span>
        <span><b>我的账本</b><small>可信、可追溯的个人财务报告</small></span>
      </RouterLink>
      <nav class="nav">
        <RouterLink to="/bill" class="nav-item">账单报告</RouterLink>
        <RouterLink to="/import" class="nav-item">导入账单</RouterLink>
      </nav>
    </header>

    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.app-header {
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
.nav {
  display: flex;
  gap: 8px;
}
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
.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  color: var(--color-text);
  text-decoration: none;
}
.brand-mark {
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
.brand b, .brand small { display: block; }
.brand small { margin-top: 2px; color: var(--color-muted); font-size: 11px; }
.app-main {
  max-width: 1128px;
  margin: 0 auto;
  padding: 24px;
}

@media (max-width: 720px) {
  .app-header { min-height: 58px; padding: 8px 16px; }
  .brand small { display: none; }
  .brand-mark { width: 34px; height: 34px; border-radius: 11px; }
  .nav-item { padding: 8px 9px; font-size: 12px; }
  .app-main { padding: 16px; }
}
</style>
