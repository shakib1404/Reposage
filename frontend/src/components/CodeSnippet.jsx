/**
 * CodeSnippet — the lines a finding is actually about.
 *
 * The backend captures a few lines either side of every located finding while
 * the clone still exists (tester.py `_attach_snippets`), because by the time a
 * report reaches the browser the workspace has been deleted. A rule id and a
 * line number alone make a reader go and look the code up; this puts it in
 * front of them with the offending line marked.
 */
export default function CodeSnippet({ code, start, line, color = 'var(--red)' }) {
  if (!code || !code.length) return null
  const first = start || 1
  const width = String(first + code.length - 1).length

  return (
    <div style={{
      border: '1px solid var(--border)', borderRadius: 6,
      overflow: 'hidden', background: 'var(--code-bg)',
    }}>
      {code.map((text, i) => {
        const n   = first + i
        const hit = n === line
        return (
          <div key={n} style={{
            display: 'flex', alignItems: 'flex-start',
            background: hit ? 'color-mix(in srgb, var(--red) 10%, transparent)' : 'transparent',
            borderLeft: `2px solid ${hit ? color : 'transparent'}`,
          }}>
            <span style={{
              fontFamily: 'var(--mono)', fontSize: 10.5, lineHeight: '18px',
              color: hit ? color : 'var(--txt3)',
              padding: '0 10px 0 8px', textAlign: 'right',
              minWidth: `${width + 1}ch`, userSelect: 'none', flexShrink: 0,
              fontWeight: hit ? 700 : 400,
            }}>
              {n}
            </span>
            <code style={{
              fontFamily: 'var(--mono)', fontSize: 11, lineHeight: '18px',
              color: hit ? 'var(--txt)' : 'var(--txt2)',
              whiteSpace: 'pre', overflowX: 'auto', paddingRight: 10,
              flex: 1, fontWeight: hit ? 600 : 400,
            }}>
              {/* A blank line still needs to occupy its row */}
              {text === '' ? ' ' : text}
            </code>
          </div>
        )
      })}
    </div>
  )
}
