/**
 * MiniMarkdown — just enough Markdown for LLM chat answers.
 *
 * The model replies in Markdown, but the chat bubble used to print the raw
 * string, so answers arrived full of literal `**`, backticks and ``` fences.
 * This renders the handful of constructs that actually show up: fenced and
 * inline code, bold, italic, bullet/numbered lists and headings.
 *
 * Everything is built as React elements — no dangerouslySetInnerHTML — so a
 * model that echoes HTML back at us can't inject anything.
 */
import { useMemo } from 'react'

// ── Inline: `code`, **bold**, *italic* ────────────────────────────────────
// One pattern with alternation so the scan stays left-to-right and a backtick
// span is claimed before any ** inside it can be.
// The double-backtick branch comes first: against ``x`` the single-backtick
// branch would match the inner `x` and leave the outer pair on screen.
const INLINE_SRC = /(``[^`\n]+``)|(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)/

function inline(text, keyPrefix) {
  // A fresh regex per call, never a shared /g one: inline() recurses into the
  // body of a bold span, and a module-level regex would have its lastIndex
  // clobbered by the inner scan mid-loop.
  const re = new RegExp(INLINE_SRC.source, 'g')
  const out = []
  let last = 0, m, i = 0

  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    const k = `${keyPrefix}-i${i++}`

    if (tok.startsWith('`')) {
      const n = tok.startsWith('``') ? 2 : 1
      out.push(
        <code key={k} style={{
          fontFamily: 'var(--mono)', fontSize: '0.92em',
          background: 'rgba(255,255,255,0.09)', padding: '1px 5px',
          borderRadius: 4, wordBreak: 'break-word',
        }}>{tok.slice(n, -n)}</code>
      )
    } else if (tok.startsWith('**')) {
      // Recurse: the models routinely write **Adapters (`adapters.py`)**, and
      // rendering the body as a plain string left those backticks visible.
      out.push(
        <strong key={k} style={{ fontWeight: 600 }}>
          {inline(tok.slice(2, -2), k)}
        </strong>
      )
    } else {
      out.push(<em key={k}>{inline(tok.slice(1, -1), k)}</em>)
    }
    last = m.index + tok.length
  }

  if (last < text.length) out.push(text.slice(last))
  return out
}

// ── Block level ───────────────────────────────────────────────────────────
function renderBlocks(src) {
  const nodes = []
  // Split on fenced code first; odd indices are the fence bodies.
  const parts = src.split(/```/)

  parts.forEach((part, pi) => {
    if (pi % 2 === 1) {
      // Fenced block — drop an optional language tag on the first line.
      const body = part.replace(/^[a-zA-Z0-9_+-]*\n/, '').replace(/\n$/, '')
      nodes.push(
        <pre key={`f${pi}`} style={{
          fontFamily: 'var(--mono)', fontSize: 11,
          background: 'rgba(0,0,0,0.38)', border: '1px solid var(--border)',
          borderRadius: 6, padding: '9px 11px', margin: '8px 0',
          overflowX: 'auto', whiteSpace: 'pre', lineHeight: 1.6,
        }}>{body}</pre>
      )
      return
    }

    // Plain prose: group consecutive list items so they share one <ul>/<ol>.
    const lines = part.split('\n')
    let list = null      // { ordered, items: [] }
    let lastWasGap = true   // start true so leading blank lines add nothing

    const flush = key => {
      if (!list) return
      const Tag = list.ordered ? 'ol' : 'ul'
      nodes.push(
        <Tag key={key} style={{ margin: '6px 0', paddingLeft: 20 }}>
          {list.items.map((it, n) => (
            <li key={n} style={{ margin: '2px 0' }}>{inline(it, `${key}-${n}`)}</li>
          ))}
        </Tag>
      )
      list = null
    }

    lines.forEach((raw, li) => {
      const key = `p${pi}l${li}`
      const line = raw.trimEnd()
      const bullet  = line.match(/^\s*[-*+]\s+(.*)$/)
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/)
      const heading = line.match(/^(#{1,4})\s+(.*)$/)

      if (bullet || ordered) {
        const isOrdered = !!ordered
        if (list && list.ordered !== isOrdered) flush(key + '-sw')
        if (!list) list = { ordered: isOrdered, items: [] }
        list.items.push((bullet || ordered)[1])
        lastWasGap = false
        return
      }
      flush(key + '-end')

      if (heading) {
        nodes.push(
          <div key={key} style={{
            fontWeight: 600, fontSize: '1.02em',
            margin: '10px 0 4px',
          }}>{inline(heading[2], key)}</div>
        )
        lastWasGap = false
      } else if (line.trim() === '') {
        // A run of blank lines is one small gap, not one gap per line.
        if (!lastWasGap) {
          nodes.push(<div key={`${key}-gap`} style={{ height: 6 }} />)
          lastWasGap = true
        }
      } else {
        nodes.push(<div key={key}>{inline(line, key)}</div>)
        lastWasGap = false
      }
    })
    flush(`p${pi}-tail`)
  })

  return nodes
}

export default function MiniMarkdown({ text = '' }) {
  const nodes = useMemo(() => renderBlocks(String(text)), [text])
  return <div style={{ wordBreak: 'break-word' }}>{nodes}</div>
}
