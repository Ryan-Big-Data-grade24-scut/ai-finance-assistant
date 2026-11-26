import type { JSONContent } from '@tiptap/core'

export function textDraft(text = ''): JSONContent {
  return {
    type: 'doc',
    content: text.split('\n').map(line => ({
      type: 'paragraph',
      ...(line ? { content: [{ type: 'text', text: line }] } : {}),
    })),
  }
}

export function readDraft(doc: JSONContent) {
  let skillId: string | undefined
  const text = (doc.content ?? []).map(paragraph => (paragraph.content ?? []).map(node => {
    if (node.type === 'skill' && typeof node.attrs?.id === 'string') skillId = node.attrs.id
    return node.type === 'text' ? node.text ?? '' : ''
  }).join('')).join('\n')
  return { text, skillId }
}

export function withoutSkill(doc: JSONContent): JSONContent {
  return { ...doc, content: doc.content?.map(paragraph => {
    const content = paragraph.content?.filter(node => node.type !== 'skill')
    return content?.length === paragraph.content?.length ? paragraph : { ...paragraph, content }
  }) }
}
