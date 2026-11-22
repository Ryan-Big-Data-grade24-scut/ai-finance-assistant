/** 账本助手共用的界面用词：页面与浮窗必须说同一套话。 */

export const KIND_LABELS: Record<string, string> = {
  init: '导入账本', uninstall: '移出账本', move: '移动', create_node: '新建分组', join: '加入净额组',
  create_group: '新建净额组', dissolve: '解散', dissolve_group: '解散净额组',
  split: '生成镜像', unsplit: '取消镜像', update: '改名/备注', undo: '撤销', redo: '重做', import: '导入账单',
}

export const STATE_LABELS: Record<string, string> = {
  pending: '待确认', confirmed: '已确认', rejected: '已拒绝', expired: '已失效',
  running: '运行中', stopping: '正在停止', completed: '本轮已结束', failed: '本轮失败',
  stopped: '已停止', interrupted: '服务中断',
}

export const TODO_LABELS: Record<string, string> = {
  pending: '待做', in_progress: '进行中', completed: '已完成',
}

export const kindLabel = (kind: string) => KIND_LABELS[kind] ?? kind
export const stateLabel = (state: string) => STATE_LABELS[state] ?? state
