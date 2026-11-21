import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // ⚠️ 这一段是必须的，不要删。
    // 前端代码里一律用相对路径 '/api/xxx'，由这里转发到后端。
    // 如果前端硬编码 http://localhost:8000，开发能跑但部署必挂。
    proxy: {
      '/api': {
        target: process.env.API_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
