<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { Extension, Node, type JSONContent } from '@tiptap/core'
import { EditorContent, useEditor } from '@tiptap/vue-3'
import Document from '@tiptap/extension-document'
import Paragraph from '@tiptap/extension-paragraph'
import Text from '@tiptap/extension-text'
import { splitBlock } from '@tiptap/pm/commands'
import { closeHistory, history, redo, undo } from '@tiptap/pm/history'
import { Fragment, Slice } from '@tiptap/pm/model'
import { TextSelection } from '@tiptap/pm/state'
import AtlasIcon from '@/components/AtlasIcon.vue'
import type { AgentSkill } from '@/api/agentCore'

const props = defineProps<{
  placeholder: string
  skills: AgentSkill[]
  skillStatus: 'loading' | 'ready' | 'error'
}>()
const draft = defineModel<JSONContent>({ required: true })
const emit = defineEmits<{ send: []; refreshSkills: [] }>()

interface Command { from: number; to: number; query: string }
const command = ref<Command | null>(null)
const highlighted = ref(0)
const menu = ref<HTMLElement | null>(null)
const menuId = useId()
const placement = ref<Record<string, string>>({})
let dismissedFrom: number | null = null
let composing = false

const matches = computed(() => {
  const query = command.value?.query.toLowerCase() ?? ''
  return props.skills.filter(skill => skill.id.toLowerCase().includes(query) || skill.label.toLowerCase().includes(query))
})

const Skill = Node.create({
  name: 'skill', group: 'inline', inline: true, atom: true, selectable: false,
  addAttributes: () => ({ id: { default: '' }, label: { default: '' } }),
  renderHTML({ node }) {
    return ['span', { class: 'ac-skill-chip ac-skill-chip--draft', contenteditable: 'false',
      'data-testid': 'ac-skill-chip', 'aria-label': `技能：${node.attrs.label}` }, node.attrs.label]
  },
})

const draftHistory = history()
const Editing = Extension.create({
  name: 'agentEditing',
  addProseMirrorPlugins: () => [draftHistory],
  addKeyboardShortcuts() {
    const run = (action: typeof undo) => () => action(this.editor.state, this.editor.view.dispatch)
    return { 'Mod-z': run(undo), 'Mod-Shift-z': run(redo), 'Mod-y': run(redo), 'Shift-Enter': run(splitBlock) }
  },
})

function activeCommand(): Command | null {
  const view = editor.value?.view
  if (!view || !view.hasFocus() || view.composing || composing) return null
  const { $from, empty } = view.state.selection
  if (!empty || !$from.parent.isTextblock) return null
  const before = $from.parent.textBetween(0, $from.parentOffset, '', '￼')
  const match = /(?:^|\s)(\/[\p{L}\p{N}_-]*)$/u.exec(before)
  if (!match) return null
  return { from: $from.pos - match[1].length, to: $from.pos, query: match[1].slice(1) }
}

function syncCommand() {
  const next = activeCommand()
  if (next?.from !== dismissedFrom) dismissedFrom = null
  command.value = next && next.from !== dismissedFrom ? next : null
}

function dismiss() {
  dismissedFrom = command.value?.from ?? null
  command.value = null
}

function choose(skill: AgentSkill) {
  const view = editor.value?.view
  const next = activeCommand()
  if (!view || !next || next.from !== command.value?.from || next.to !== command.value.to) return
  const { state } = view
  const tr = closeHistory(state.tr)
  state.doc.descendants((node, pos) => {
    if (node.type.name === 'skill') tr.delete(pos, pos + node.nodeSize)
  })
  const from = tr.mapping.map(next.from)
  const to = tr.mapping.map(next.to)
  const chip = state.schema.nodes.skill.create({ id: skill.id, label: skill.label })
  tr.replaceWith(from, to, chip).setSelection(TextSelection.create(tr.doc, from + chip.nodeSize))
  dismiss()
  view.dispatch(tr.scrollIntoView())
  view.focus()
}

function positionMenu() {
  if (!command.value || !editor.value || !menu.value) return
  const rect = editor.value.view.dom.getBoundingClientRect()
  const width = Math.min(365, Math.max(260, rect.width), window.innerWidth - 16)
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))
  const above = Math.max(0, rect.top - 14)
  const below = Math.max(0, window.innerHeight - rect.bottom - 14)
  const useAbove = above >= Math.min(220, below)
  placement.value = {
    left: `${left}px`, width: `${width}px`, maxHeight: `${Math.min(300, useAbove ? above : below)}px`,
    ...(useAbove ? { bottom: `${window.innerHeight - rect.top + 6}px` } : { top: `${rect.bottom + 6}px` }),
  }
}

const editor = useEditor({
  content: draft.value,
  extensions: [Document, Paragraph, Text, Skill, Editing],
  editorProps: {
    attributes: { class: 'ac-editor', 'data-testid': 'ac-input', role: 'textbox',
      'aria-label': '给 ATLAS 的消息', 'aria-multiline': 'true', 'aria-autocomplete': 'list' },
    handleDOMEvents: {
      compositionstart: () => { composing = true; command.value = null; return false },
      compositionend: () => { composing = false; setTimeout(syncCommand, 0); return false },
    },
    handlePaste(view, event) {
      const text = event.clipboardData?.getData('text/plain') ?? ''
      if (text) {
        const { schema } = view.state
        const lines = text.replace(/\r\n?/g, '\n').split('\n')
        const blocks = lines.map(line => schema.nodes.paragraph.create(null, line ? schema.text(line) : null))
        const slice = Slice.maxOpen(Fragment.fromArray(blocks))
        view.dispatch(closeHistory(view.state.tr).replaceSelection(slice).scrollIntoView())
      }
      return true
    },
    handleKeyDown(view, event) {
      if (event.isComposing || event.keyCode === 229 || view.composing || composing) return false
      if (command.value) {
        if (event.key === 'Escape') {
          event.stopPropagation()
          dismiss()
          return true
        }
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
          const count = matches.value.length
          if (count) highlighted.value = (highlighted.value + (event.key === 'ArrowUp' ? -1 : 1) + count) % count
          return true
        }
        if (event.key === 'Enter' && !event.shiftKey) {
          const skill = matches.value[highlighted.value]
          if (skill && props.skillStatus === 'ready') choose(skill)
          return true
        }
      }
      if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
        emit('send')
        return true
      }
      const { $from, empty } = view.state.selection
      if (empty && (event.key === 'Backspace' || event.key === 'Delete')) {
        const before = event.key === 'Backspace'
        const adjacent = before ? $from.nodeBefore : $from.nodeAfter
        if (adjacent?.type.name === 'skill') {
          const pos = $from.pos - (before ? adjacent.nodeSize : 0)
          view.dispatch(closeHistory(view.state.tr).delete(pos, pos + adjacent.nodeSize))
          return true
        }
      }
      return false
    },
  },
  onUpdate: ({ editor }) => { draft.value = editor.getJSON() },
  onTransaction: syncCommand,
  onFocus: syncCommand,
  onBlur: () => { command.value = null },
})

watch(draft, next => {
  const instance = editor.value
  if (!instance || JSON.stringify(next) === JSON.stringify(instance.getJSON())) return
  // 外部替换（例如发送成功）开启新草稿；撤销不能恢复已经发送的内容。
  instance.unregisterPlugin(draftHistory.spec.key!)
  instance.commands.setContent(next, { emitUpdate: false })
  instance.registerPlugin(draftHistory)
  command.value = null
})

watch(() => [command.value?.query, command.value?.from, props.skillStatus], async () => {
  highlighted.value = 0
  await nextTick()
  positionMenu()
})
watch(highlighted, async index => {
  await nextTick()
  document.getElementById(`${menuId}-${index}`)?.scrollIntoView({ block: 'nearest' })
})
watch(() => [editor.value, command.value, highlighted.value, props.skillStatus], () => {
  const element = editor.value?.view.dom
  if (!element) return
  if (command.value) element.setAttribute('aria-controls', menuId)
  else element.removeAttribute('aria-controls')
  if (command.value && matches.value[highlighted.value] && props.skillStatus === 'ready') {
    element.setAttribute('aria-activedescendant', `${menuId}-${highlighted.value}`)
  } else element.removeAttribute('aria-activedescendant')
}, { flush: 'post' })

onMounted(() => {
  window.addEventListener('resize', positionMenu)
  window.addEventListener('scroll', positionMenu, true)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', positionMenu)
  window.removeEventListener('scroll', positionMenu, true)
})

defineExpose({ focus: () => editor.value?.commands.focus('end') })
</script>

<template>
  <div class="ac-editor-wrap">
    <span v-if="editor?.isEmpty" class="ac-editor-placeholder" aria-hidden="true">{{ placeholder }}</span>
    <EditorContent :editor="editor" />
    <Teleport to="body">
      <div v-if="command" ref="menu" class="ac-skill-menu atlas-theme" :style="placement"
           data-testid="ac-skill-menu" @mousedown.prevent>
        <div class="ac-skill-heading">技能 <kbd>↑ ↓</kbd> 选择 <kbd>Enter</kbd> 确认</div>
        <div :id="menuId" role="listbox" aria-label="可用技能">
          <div v-for="(skill, index) in skillStatus === 'ready' ? matches : []" :id="`${menuId}-${index}`"
               :key="skill.id" class="ac-skill-option" role="option" :aria-selected="index === highlighted"
               :title="skill.when_to_use || skill.description" @mousemove="highlighted = index" @click="choose(skill)">
            <AtlasIcon name="list" />
            <span><b>{{ skill.label }}</b><small>{{ skill.description || `/${skill.id}` }}</small></span>
          </div>
        </div>
        <p v-if="skillStatus === 'loading'" role="status">正在读取技能…</p>
        <p v-else-if="skillStatus === 'error'" role="status">技能列表暂不可用。
          <button type="button" @click="emit('refreshSkills')">重试</button>
        </p>
        <p v-else-if="!matches.length" role="status">没有匹配的技能</p>
      </div>
    </Teleport>
  </div>
</template>

<style scoped src="./AgentCoreComposer.css"></style>
