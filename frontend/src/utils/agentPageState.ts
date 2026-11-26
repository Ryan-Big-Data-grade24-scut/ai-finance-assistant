/**
 * 当前页面公开给浮窗助手的上下文。
 *
 * 只放稳定引用（路由、日期范围、选区），不放金额和行内容；助手每轮都会
 * 用工具重新读账本。模块级单例让浮窗与完整页面读同一份事实，不引入 Pinia。
 */
import { reactive, ref } from 'vue'

export const agentLedgerRevision = ref(0)

export const agentPageState = reactive({
  route: '',
  dateFrom: '',
  dateTo: '',
  selection: [] as string[],
})

export function setAgentPageRoute(route: string) {
  agentPageState.route = route
}

export function setAgentPageRange(dateFrom: string, dateTo: string) {
  agentPageState.dateFrom = dateFrom
  agentPageState.dateTo = dateTo
}

export function setAgentPageSelection(selection: string[]) {
  agentPageState.selection = selection
}

/** 离开页面时清掉页面独有的上下文，避免浮窗把上一页的选区带进新页面。 */
export function clearAgentPage() {
  agentPageState.route = ''
  agentPageState.dateFrom = ''
  agentPageState.dateTo = ''
  agentPageState.selection = []
}
