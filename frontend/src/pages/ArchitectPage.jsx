import { useState, useEffect, useRef, useCallback } from 'react'
import mermaid from 'mermaid'
import { streamArchitect } from '../api'
import MiniMarkdown from '../components/MiniMarkdown'

// ── Mermaid initialisation ────────────────────────────────────────────────────
mermaid.initialize({
  startOnLoad:  false,
  theme:        'dark',
  darkMode:     true,
  flowchart:    { curve: 'basis', useMaxWidth: true },
  themeVariables: {
    background:        '#0d1117',
    mainBkg:           '#161b22',
    nodeBorder:        '#30363d',
    clusterBkg:        '#161b22',
    titleColor:        '#e6edf3',
    edgeLabelBackground: '#161b22',
    lineColor:         '#8b949e',
  },
})

// ── Stage metadata ────────────────────────────────────────────────────────────
const STAGE_ORDER = ['fetching', 'explanation', 'graph', 'compiling', 'done']
const STAGE_LABEL = {
  fetching:    'Fetching repository',
  explanation: 'Explaining architecture',
  graph:       'Building graph',
  graph_retry: 'Retrying graph',
  compiling:   'Compiling diagram',
  done:        'Complete',
}
const STAGE_ICON = {
  fetching:    '🔗',
  explanation: '🧠',
  graph:       '🕸',
  graph_retry: '🔄',
  compiling:   '⚙',
  done:        '✅',
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusFeed({ events }) {
  const bottomRef = useRef(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  return (
    <div style={{
      background: 'var(--bg2)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)', padding: '14px 18px',
      fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 2,
      maxHeight: 220, overflowY: 'auto',
    }}>
      {events.map((ev, i) => (
        <div key={i} style={{
          display: 'flex', alignItems: 'flex-start', gap: 8,
          color: ev.type === 'error' ? 'var(--red)' : 'var(--txt2)',
        }}>
          <span style={{ flexShrink: 0, width: 18 }}>
            {STAGE_ICON[ev.stage] || (ev.type === 'error' ? '✕' : '·')}
          </span>
          <span>{ev.message}</span>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}

function ProgressBar({ stage }) {
  const idx  = STAGE_ORDER.indexOf(stage)
  const pct  = idx === -1 ? 10 : Math.round(((idx + 1) / STAGE_ORDER.length) * 100)
  const done = stage === 'done'
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 12, color: done ? 'var(--green)' : 'var(--txt2)' }}>
          {STAGE_LABEL[stage] || stage}
        </span>
        <span style={{ fontSize: 11, color: 'var(--txt3)' }}>{pct}%</span>
      </div>
      <div style={{ height: 3, background: 'var(--bg3)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%', width: pct + '%',
          background: done ? 'var(--green)' : 'var(--accent)',
          borderRadius: 2, transition: 'width 0.4s ease',
        }} />
      </div>
    </div>
  )
}

function MermaidDiagram({ code }) {
  const containerRef = useRef(null)
  const [error,  setError]  = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!code || !containerRef.current) return
    setError('')
    const id = `mermaid-${Date.now()}`
    mermaid.render(id, code)
      .then(({ svg }) => {
        if (containerRef.current) {
          containerRef.current.innerHTML = svg
          // Make SVG responsive
          const svgEl = containerRef.current.querySelector('svg')
          if (svgEl) {
            svgEl.style.maxWidth = '100%'
            svgEl.style.height   = 'auto'
          }
        }
      })
      .catch(err => {
        setError(err?.message || 'Mermaid render failed')
      })
  }, [code])

  const handleCopy = () => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    })
  }

  if (error) {
    return (
      <div>
        <div style={{
          padding: '10px 14px', background: '#2d1b1b',
          border: '1px solid var(--red)', borderRadius: 'var(--radius)',
          color: 'var(--red)', fontSize: 12, marginBottom: 10,
        }}>
          Mermaid render error: {error}
        </div>
        <pre style={{
          background: 'var(--bg3)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius)', padding: 12,
          fontSize: 11, fontFamily: 'var(--mono)',
          overflowX: 'auto', color: 'var(--txt2)',
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>{code}</pre>
      </div>
    )
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8, gap: 8 }}>
        <button
          onClick={handleCopy}
          style={{
            display: 'flex', alignItems: 'center', gap: 5,
            padding: '5px 12px', fontSize: 11,
            border: '1px solid var(--border2)', borderRadius: 5,
            background: 'transparent', color: 'var(--txt2)',
            cursor: 'pointer',
          }}
          onMouseOver={e => { e.currentTarget.style.borderColor = 'var(--accent)'; e.currentTarget.style.color = 'var(--accent)' }}
          onMouseOut={e  => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--txt2)' }}
        >
          {copied ? '✓ Copied' : 'Copy Mermaid'}
        </button>
        <button
          onClick={() => {
            const blob = new Blob([code], { type: 'text/plain' })
            const a    = document.createElement('a')
            a.href     = URL.createObjectURL(blob)
            a.download = 'architecture.mmd'
            a.click()
          }}
          style={{
            display: 'flex', alignItems: 'center', gap: 5,
            padding: '5px 12px', fontSize: 11,
            border: '1px solid var(--border2)', borderRadius: 5,
            background: 'transparent', color: 'var(--txt2)',
            cursor: 'pointer',
          }}
          onMouseOver={e => { e.currentTarget.style.borderColor = 'var(--accent)'; e.currentTarget.style.color = 'var(--accent)' }}
          onMouseOut={e  => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--txt2)' }}
        >
          Download .mmd
        </button>
      </div>

      <div
        ref={containerRef}
        style={{
          background: '#0d1117',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '20px',
          overflowX: 'auto',
          minHeight: 200,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'flex-start',
        }}
      />
    </div>
  )
}

function ExplanationAccordion({ text }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{
      background: 'var(--bg2)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)', marginTop: 12, overflow: 'hidden',
    }}>
      <button
        onClick={() => setOpen(v => !v)}
        style={{
          width: '100%', padding: '10px 16px',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          background: 'transparent', border: 'none',
          color: 'var(--txt2)', fontSize: 12, fontFamily: 'var(--font)',
          cursor: 'pointer',
        }}
      >
        <span style={{ fontWeight: 500 }}>Architecture explanation (LLM)</span>
        <span style={{ fontSize: 14, transition: 'transform 0.2s', transform: open ? 'rotate(180deg)' : 'none' }}>▾</span>
      </button>
      {open && (
        <div style={{
          padding: '0 16px 14px',
          fontSize: 12, color: 'var(--txt2)', lineHeight: 1.7,
          borderTop: '1px solid var(--border)',
          paddingTop: 12,
        }}>
          {/* The model answers in Markdown. Printed as a literal string this
              read as one long wall of `**heading**` and backticks — 34 stray
              `**` and 180 backticks on psf/requests alone. */}
          <MiniMarkdown text={text} />
        </div>
      )}
    </div>
  )
}

function GraphStats({ graph }) {
  if (!graph) return null
  const nodes  = (graph.nodes  || []).length
  const edges  = (graph.edges  || []).length
  const groups = (graph.groups || []).length
  return (
    <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
      {[
        { label: 'Nodes',  value: nodes  },
        { label: 'Edges',  value: edges  },
        { label: 'Groups', value: groups },
      ].map(({ label, value }) => (
        <div key={label} style={{
          background: 'var(--bg2)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius)', padding: '7px 16px',
          textAlign: 'center',
        }}>
          <div style={{ fontSize: 10, color: 'var(--txt3)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</div>
          <div style={{ fontSize: 18, fontWeight: 600 }}>{value}</div>
        </div>
      ))}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function ArchitectPage({
  selectedRepo, architectResult, setArchitectResult, unlock, go,
}) {
  const [running,    setRunning]    = useState(false)
  const [stage,      setStage]      = useState('idle')
  const [feedEvents, setFeedEvents] = useState([])
  const [error,      setError]      = useState('')
  const stopRef = useRef(null)

  const addFeed = useCallback((msg, stageKey, type = 'status') => {
    setFeedEvents(prev => [...prev, { message: msg, stage: stageKey, type }])
  }, [])

  const handleGenerate = useCallback(() => {
    if (!selectedRepo) return
    setRunning(true)
    setError('')
    setFeedEvents([])
    setStage('fetching')
    setArchitectResult(null)

    const stop = streamArchitect(selectedRepo.full_name, (ev) => {
      if (ev.type === 'status') {
        setStage(ev.stage)
        addFeed(ev.message, ev.stage)
      } else if (ev.type === 'explanation') {
        addFeed('Architecture explanation generated.', 'explanation')
      } else if (ev.type === 'graph') {
        addFeed('Architecture graph built.', 'graph')
      } else if (ev.type === 'done') {
        setStage('done')
        addFeed('Diagram ready!', 'done')
        setArchitectResult({
          mermaid:     ev.mermaid,
          explanation: ev.explanation,
          graph:       ev.graph,
          branch:      ev.branch,
          tokens:      ev.tokens,
        })
        unlock('execute')
        setRunning(false)
      } else if (ev.type === 'error') {
        setError(ev.message)
        setStage('error')
        addFeed(ev.message, 'error', 'error')
        setRunning(false)
      }
    })

    stopRef.current = stop
  }, [selectedRepo, addFeed, setArchitectResult, unlock])

  useEffect(() => {
    return () => {
      if (stopRef.current) stopRef.current()
    }
  }, [])

  if (!selectedRepo) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: 'var(--txt3)' }}>
        No repository selected. Go back and select a repo first.
      </div>
    )
  }

  return (
    <div style={{ padding: '18px 22px', maxWidth: 1100, margin: '0 auto' }} className="fade-in">

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18 }}>
        <div>
          <h2 style={{ fontSize: 15, fontWeight: 600, margin: 0, letterSpacing: '-0.01em' }}>
            Architecture Diagram
          </h2>
          <p style={{ fontSize: 12, color: 'var(--txt2)', margin: '2px 0 0' }}>
            {selectedRepo.full_name} — AI-generated Mermaid architecture
          </p>
        </div>
        <div style={{ flex: 1 }} />
        {architectResult && (
          <button
            onClick={() => { unlock('execute'); go('execute') }}
            style={{
              padding: '8px 16px', background: 'var(--accent)',
              border: 'none', borderRadius: 'var(--radius)',
              color: 'white', fontSize: 13, fontWeight: 500, cursor: 'pointer',
            }}
          >
            Execute task →
          </button>
        )}
      </div>

      {/* ── Generate button or progress ──────────────────────────────── */}
      {!architectResult && !running && (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>🏗</div>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>
            Generate Architecture Diagram
          </div>
          <div style={{ fontSize: 12, color: 'var(--txt2)', marginBottom: 24, maxWidth: 420, margin: '0 auto 24px' }}>
            RepoSage will fetch the file tree from GitHub, ask the LLM to explain the architecture,
            then compile it into an interactive Mermaid flowchart.
          </div>
          {error && (
            <div style={{
              background: '#2d1b1b', border: '1px solid var(--red)',
              borderRadius: 'var(--radius)', padding: '10px 16px',
              color: 'var(--red)', fontSize: 12,
              marginBottom: 20, maxWidth: 480, margin: '0 auto 20px',
            }}>
              {error}
            </div>
          )}
          <button
            onClick={handleGenerate}
            style={{
              padding: '10px 28px', background: 'var(--accent)',
              border: 'none', borderRadius: 'var(--radius)',
              color: 'white', fontSize: 14, fontWeight: 600,
              cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 8,
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            Generate diagram
          </button>
        </div>
      )}

      {running && (
        <div style={{ maxWidth: 580, margin: '0 auto', padding: '20px 0' }}>
          <ProgressBar stage={stage} />
          <StatusFeed events={feedEvents} />
          <div style={{ textAlign: 'center', marginTop: 12 }}>
            <button
              onClick={() => { if (stopRef.current) stopRef.current(); setRunning(false); setStage('idle') }}
              style={{
                padding: '5px 14px', fontSize: 11,
                border: '1px solid var(--border2)', borderRadius: 5,
                background: 'transparent', color: 'var(--txt3)', cursor: 'pointer',
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* ── Result ───────────────────────────────────────────────────── */}
      {architectResult && (
        <div className="fade-in">
          {/* Stats row */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 12 }}>
            <GraphStats graph={architectResult.graph} />
            <div style={{ flex: 1 }} />
            {architectResult.tokens > 0 && (
              <span style={{ fontSize: 11, color: 'var(--txt3)' }}>
                {architectResult.tokens.toLocaleString()} tokens used
              </span>
            )}
            <button
              onClick={handleGenerate}
              style={{
                display: 'flex', alignItems: 'center', gap: 5,
                padding: '5px 12px', fontSize: 11,
                border: '1px solid var(--border2)', borderRadius: 5,
                background: 'transparent', color: 'var(--txt2)',
                cursor: 'pointer',
              }}
              onMouseOver={e => { e.currentTarget.style.borderColor = 'var(--accent)'; e.currentTarget.style.color = 'var(--accent)' }}
              onMouseOut={e  => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--txt2)' }}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4"/>
              </svg>
              Regenerate
            </button>
          </div>

          {/* Diagram */}
          <div style={{
            background: 'var(--bg2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius-lg)', padding: '16px 18px',
          }}>
            <div style={{
              fontSize: 11, fontWeight: 500, color: 'var(--txt2)',
              textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12,
              display: 'flex', alignItems: 'center', gap: 6,
            }}>
              <span style={{
                background: 'var(--accent-dim)', border: '1px solid rgba(93,142,255,0.3)',
                borderRadius: 3, padding: '1px 7px', color: 'var(--accent)',
              }}>
                Mermaid Flowchart
              </span>
              <span>Architecture</span>
              {architectResult.branch && (
                <span style={{ color: 'var(--txt3)', fontWeight: 400 }}>
                  · branch: {architectResult.branch}
                </span>
              )}
            </div>
            <MermaidDiagram code={architectResult.mermaid} />
          </div>

          {/* Explanation */}
          {architectResult.explanation && (
            <ExplanationAccordion text={architectResult.explanation} />
          )}
        </div>
      )}
    </div>
  )
}
