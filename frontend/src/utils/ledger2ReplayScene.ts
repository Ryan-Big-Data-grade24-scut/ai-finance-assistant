import type { Ledger2ReplayItem, Ledger2ReplayStep } from '@/api/ledger2'

export function describeReplay(step: Ledger2ReplayStep) {
  const before = new Map(step.before.map(item => [item.ref, item]))
  const after = new Map(step.after.map(item => [item.ref, item]))
  const changed = new Set(step.changed)
  const created = step.after.filter(item => item.kind !== 'tx' && !before.has(item.ref) && changed.has(item.ref))
  const removed = step.before.filter(item => item.kind !== 'tx' && !after.has(item.ref) && changed.has(item.ref))
  const moves = step.changed.flatMap(ref => {
    const from = before.get(ref)
    const to = after.get(ref)
    return from && to && from.parent !== to.parent ? [{ ref, from, to }] : []
  })
  const routes = new Map(moves.map(({ from, to }) => [
    JSON.stringify([from.parent, to.parent]),
    { from: before.get(from.parent ?? '')?.name ?? '总账', to: after.get(to.parent ?? '')?.name ?? '总账' },
  ]))
  const rename = step.changed.map(ref => [before.get(ref), after.get(ref)] as const)
    .find(([from, to]) => from && to && from.name !== to.name)
  const names = (items: Ledger2ReplayItem[]) => items.length === 1 ? items[0].name : items.length + ' 个分组'
  const route = routes.values().next().value
  const summary = created.length ? '新建 · ' + names(created)
    : removed.length ? '解散 · ' + names(removed)
    : routes.size > 1 ? routes.size + ' 条移动路径'
    : route ? route.from + ' → ' + route.to
    : rename ? (rename[0]?.name ?? '') + ' → ' + (rename[1]?.name ?? '')
    : step.changed.length ? '更新节点' : '本步无可见变化'
  return {
    summary,
    sources: new Set(moves.flatMap(({ from }) => from.parent ? [from.parent] : [])),
    preview: !!(created.length || moves.length),
    settle: !!(removed.length && moves.length),
    removing: new Set(removed.map(item => item.ref)),
  }
}

/** Keep large batches of identical transfers readable on the star map. */
export function replayScene(step: Ledger2ReplayStep) {
  const before = new Map(step.before.map(item => [item.ref, item]))
  const after = new Map(step.after.map(item => [item.ref, item]))
  const changed = new Set(step.changed)
  const targets = new Set<string>()
  const routes = new Map<string, string[]>()
  const bundles: Array<{ key: string; from: string; to: string; names: string[] }> = []

  for (const ref of changed) {
    const source = before.get(ref)
    const target = after.get(ref)
    if (target?.parent && source?.parent !== target.parent) targets.add(target.parent)
    if ((source ?? target)?.kind !== 'tx' || source?.parent === target?.parent) continue
    const key = JSON.stringify([source?.parent, target?.parent])
    const refs = routes.get(key) ?? []
    refs.push(ref)
    routes.set(key, refs)
  }

  for (const [key, refs] of routes) {
    if (refs.length <= 4) continue
    const ref = `replay:${key}`
    const source = before.get(refs[0])
    const target = after.get(refs[0])
    bundles.push({ key, from: source ? before.get(source.parent ?? '')?.name ?? '总账' : '未放入账本',
      to: target ? after.get(target.parent ?? '')?.name ?? '总账' : '移出账本',
      names: refs.map(id => (before.get(id) ?? after.get(id))!.name) })
    for (const items of [before, after]) {
      const txs = refs.map(id => items.get(id)).filter((item): item is Ledger2ReplayItem => !!item)
      if (txs.length) {
        items.set(ref, { ...txs[0], ref, name: `${txs.length} 笔交易`,
          note: txs.slice(0, 8).map(item => item.name).join('、') + (txs.length > 8 ? '…' : '') })
      }
      for (const id of refs) items.delete(id)
    }
    for (const id of refs) changed.delete(id)
    changed.add(ref)
  }

  return { before, after, changed, targets, bundles }
}

/** Reserve a position for each distinct ancestry path on both sides of the change. */
export function replaySlots(before: Map<string, Ledger2ReplayItem>, after: Map<string, Ledger2ReplayItem>) {
  const slots = new Map<string, { item: Ledger2ReplayItem; parent: string | null }>()
  const phaseKeys = (items: typeof before, other: typeof after) => {
    const paths = new Map<string, string[]>()
    const visiting = new Set<string>()
    function pathOf(item: Ledger2ReplayItem): string[] {
      const known = paths.get(item.ref)
      if (known) return known
      visiting.add(item.ref)
      const parent = item.parent ? items.get(item.parent) ?? other.get(item.parent) : undefined
      const upper = parent && !visiting.has(parent.ref) ? pathOf(parent) : []
      visiting.delete(item.ref)
      const path = [...upper, item.ref]
      paths.set(item.ref, path)
      slots.set(slotKey(path), { item, parent: upper.length ? slotKey(upper) : null })
      return path
    }
    return new Map([...items.values()].map(item => [item.ref, slotKey(pathOf(item))]))
  }
  return { slots, beforeKeys: phaseKeys(before, after), afterKeys: phaseKeys(after, before) }
}

function slotKey(path: string[]) {
  return path.length === 1 && path[0] === 'root' ? 'root' : JSON.stringify(path)
}
