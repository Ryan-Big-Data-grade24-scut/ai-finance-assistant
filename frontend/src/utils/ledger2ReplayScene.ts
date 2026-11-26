import type { Ledger2ReplayItem, Ledger2ReplayStep } from '@/api/ledger2'

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
