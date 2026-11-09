/** Small, dependency-free Markdown renderer for trusted model text.
 * Raw HTML is always escaped. Only http(s) links are emitted as anchors.
 */
function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

function renderInline(source: string): string {
  let text = escapeHtml(source)
  const code: string[] = []
  text = text.replace(/`([^`]+)`/g, (_match, value: string) => {
    const token = `\u0000CODE${code.length}\u0000`
    code.push(`<code>${value}</code>`)
    return token
  })
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_match, label: string, url: string) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`)
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/__(.+?)__/g, '<strong>$1</strong>')
  text = text.replace(/~~(.+?)~~/g, '<del>$1</del>')
  text = text.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
  return text.replace(/\u0000CODE(\d+)\u0000/g, (_match, index: string) => code[Number(index)] || '')
}

function splitTableRow(line: string): string[] {
  const escapedPipe = '\u0000PIPE\u0000'
  let value = line.trim().replace(/\\\|/g, escapedPipe)
  if (value.startsWith('|')) value = value.slice(1)
  if (value.endsWith('|')) value = value.slice(0, -1)
  return value.split('|').map(cell => cell.split(escapedPipe).join('|').trim())
}

function isTableSeparator(line: string): boolean {
  const cells = splitTableRow(line)
  return cells.length >= 2 && cells.every(cell => /^:?-{3,}:?$/.test(cell))
}

function renderTable(headerLine: string, bodyLines: string[]): string {
  const headers = splitTableRow(headerLine)
  const rows = bodyLines.map(splitTableRow)
  const head = `<thead><tr>${headers.map(cell => `<th>${renderInline(cell)}</th>`).join('')}</tr></thead>`
  const body = `<tbody>${rows.map(row => {
    const cells = headers.map((_header, index) => `<td>${renderInline(row[index] || '')}</td>`)
    return `<tr>${cells.join('')}</tr>`
  }).join('')}</tbody>`
  return `<table>${head}${body}</table>`
}

export function renderMarkdown(source: string): string {
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n')
  const output: string[] = []
  let paragraph: string[] = []
  let listType: 'ul' | 'ol' | null = null
  let listItems: string[] = []
  let quoteLines: string[] = []
  let codeLines: string[] | null = null

  const flushParagraph = () => {
    if (paragraph.length) {
      output.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`)
      paragraph = []
    }
  }
  const flushList = () => {
    if (listType && listItems.length) output.push(`<${listType}>${listItems.map(item => `<li>${renderInline(item)}</li>`).join('')}</${listType}>`)
    listType = null
    listItems = []
  }
  const flushQuote = () => {
    if (quoteLines.length) {
      output.push(`<blockquote>${quoteLines.map(renderInline).join('<br>')}</blockquote>`)
      quoteLines = []
    }
  }
  const flushCode = () => {
    if (codeLines !== null) {
      output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
      codeLines = null
    }
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    if (/^\s*```/.test(line)) {
      flushParagraph(); flushList(); flushQuote()
      if (codeLines === null) codeLines = []
      else flushCode()
      continue
    }
    if (codeLines !== null) {
      codeLines.push(line)
      continue
    }

    // GitHub-flavoured Markdown table: header row followed by |---|---|.
    if (line.includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      flushParagraph(); flushList(); flushQuote()
      const bodyLines: string[] = []
      index += 2
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        bodyLines.push(lines[index])
        index += 1
      }
      output.push(renderTable(line, bodyLines))
      index -= 1
      continue
    }

    if (!line.trim()) {
      flushParagraph(); flushList(); flushQuote()
      continue
    }
    const heading = line.match(/^\s*(#{1,4})\s+(.+)$/)
    if (heading) {
      flushParagraph(); flushList(); flushQuote()
      const level = heading[1].length
      output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`)
      continue
    }
    if (/^\s*(---+|___+|\*\*\*+)\s*$/.test(line)) {
      flushParagraph(); flushList(); flushQuote(); output.push('<hr>')
      continue
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/)
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/)
    if (unordered || ordered) {
      flushParagraph(); flushQuote()
      const nextType = unordered ? 'ul' : 'ol'
      if (listType && listType !== nextType) flushList()
      listType = nextType
      listItems.push((unordered || ordered)![1])
      continue
    }
    const quote = line.match(/^\s*>\s?(.*)$/)
    if (quote) {
      flushParagraph(); flushList()
      quoteLines.push(quote[1])
      continue
    }
    flushList(); flushQuote()
    paragraph.push(line)
  }
  flushParagraph(); flushList(); flushQuote(); flushCode()
  return output.join('')
}
