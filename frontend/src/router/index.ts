import { createRouter, createWebHistory } from 'vue-router'
import BillView from '@/views/BillView.vue'

/**
 * 路由契约 —— 只有你能改
 *
 * query 参数名必须和后端接口的参数名一模一样：
 *   /bill?type=expense&period=month&value=2026-09
 *         ↑ 与 GET /api/report/summary?type=&period=&value= 对应
 *
 * 改这里就要同步改后端的参数名，否则前后端对不上。
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/bill' },
    {
      path: '/bill',
      name: 'bill',
      component: BillView,
      // 默认值：支出 · 月度 · 9月 · 小结
      props: route => ({
        type: (route.query.type as string) ?? 'expense',
        period: (route.query.period as string) ?? 'month',
        value: (route.query.value as string) ?? '2026-10',
        tab: (route.query.tab as string) ?? 'summary',
      }),
    },
    {
      path: '/import',
      name: 'import',
      component: () => import('@/views/ImportView.vue'),
    },
    {
      path: '/tx',
      name: 'tx',
      component: () => import('@/views/TxEditView.vue'),
      props: route => ({ id: route.query.id }),
    },
    {
      path: '/transactions',
      name: 'transactions',
      component: () => import('@/views/TransactionsView.vue'),
    },
  ],
})

export default router
