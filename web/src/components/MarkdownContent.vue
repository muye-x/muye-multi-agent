<script setup lang="ts">
import { computed, h, type VNode } from 'vue'

type MarkdownBlock =
  | { type: 'heading'; level: number; content: string }
  | { type: 'paragraph'; content: string }
  | { type: 'quote'; content: string }
  | { type: 'code'; language: string; content: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'table'; headers: string[]; rows: string[][] }
  | { type: 'rule' }

const props = defineProps<{ content: string }>()

function parseBlocks(content: string): MarkdownBlock[] {
  const lines = content.replace(/\r\n/g, '\n').split('\n')
  const blocks: MarkdownBlock[] = []
  let paragraph: string[] = []

  const flushParagraph = (): void => {
    if (paragraph.length) blocks.push({ type: 'paragraph', content: paragraph.join('\n') })
    paragraph = []
  }

  const splitTableRow = (line: string): string[] => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
  const isTableSeparator = (line: string): boolean => splitTableRow(line).every((cell) => /^:?-{3,}:?$/.test(cell))

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const codeStart = line.match(/^```([^`]*)$/)
    if (codeStart) {
      flushParagraph()
      const code: string[] = []
      index += 1
      while (index < lines.length && !lines[index].startsWith('```')) {
        code.push(lines[index])
        index += 1
      }
      blocks.push({ type: 'code', language: codeStart[1].trim(), content: code.join('\n') })
      continue
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      flushParagraph()
      blocks.push({ type: 'heading', level: heading[1].length, content: heading[2] })
      continue
    }
    if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      flushParagraph()
      const headers = splitTableRow(line)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        rows.push(splitTableRow(lines[index]))
        index += 1
      }
      index -= 1
      blocks.push({ type: 'table', headers, rows })
      continue
    }
    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      flushParagraph()
      blocks.push({ type: 'rule' })
      continue
    }
    const quote = line.match(/^>\s?(.*)$/)
    if (quote) {
      flushParagraph()
      blocks.push({ type: 'quote', content: quote[1] })
      continue
    }
    const unordered = line.match(/^[-*+]\s+(.+)$/)
    const ordered = line.match(/^\d+[.)]\s+(.+)$/)
    if (unordered || ordered) {
      flushParagraph()
      const isOrdered = Boolean(ordered)
      const items = [(unordered || ordered)![1]]
      const marker = isOrdered ? /^\d+[.)]\s+(.+)$/ : /^[-*+]\s+(.+)$/
      while (index + 1 < lines.length && marker.test(lines[index + 1])) {
        index += 1
        items.push(lines[index].match(marker)![1])
      }
      blocks.push({ type: 'list', ordered: isOrdered, items })
      continue
    }
    if (!line.trim()) {
      flushParagraph()
      continue
    }
    paragraph.push(line)
  }
  flushParagraph()
  return blocks
}

function inlineNodes(content: string): VNode[] {
  const pattern = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^\s)]+\)|\*[^*]+\*|_[^_]+_)/g
  const nodes: VNode[] = []
  let cursor = 0
  for (const match of content.matchAll(pattern)) {
    const offset = match.index ?? 0
    if (offset > cursor) nodes.push(h('span', content.slice(cursor, offset)))
    const token = match[0]
    if ((token.startsWith('**') && token.endsWith('**')) || (token.startsWith('__') && token.endsWith('__'))) {
      nodes.push(h('strong', token.slice(2, -2)))
    } else if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(h('code', token.slice(1, -1)))
    } else if (token.startsWith('[')) {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/)
      if (link) nodes.push(h('a', { href: link[2], target: '_blank', rel: 'noopener noreferrer' }, link[1]))
      else nodes.push(h('span', token))
    } else {
      nodes.push(h('em', token.slice(1, -1)))
    }
    cursor = offset + token.length
  }
  if (cursor < content.length) nodes.push(h('span', content.slice(cursor)))
  return nodes
}

const blocks = computed(() => parseBlocks(props.content))

const RenderMarkdown = (): VNode => h('div', { class: 'markdown-content' }, blocks.value.map((block) => {
  if (block.type === 'heading') return h(`h${block.level}`, inlineNodes(block.content))
  if (block.type === 'paragraph') return h('p', inlineNodes(block.content))
  if (block.type === 'quote') return h('blockquote', inlineNodes(block.content))
  if (block.type === 'code') return h('pre', [h('code', { class: block.language ? `language-${block.language}` : undefined }, block.content)])
  if (block.type === 'list') return h(block.ordered ? 'ol' : 'ul', block.items.map((item) => h('li', inlineNodes(item))))
  if (block.type === 'table') return h('div', { class: 'markdown-table-wrap' }, [h('table', [
    h('thead', [h('tr', block.headers.map((header) => h('th', inlineNodes(header))))]),
    h('tbody', block.rows.map((row) => h('tr', block.headers.map((_, index) => h('td', inlineNodes(row[index] || '')))))),
  ])])
  return h('hr')
}))
</script>

<template>
  <RenderMarkdown />
</template>
