import { useState, useEffect, useRef, useCallback } from 'react'
import mermaid from 'mermaid'
import { streamArchitect, getArchitectKinds } from '../api'
import MiniMarkdown from '../components/MiniMarkdown'
import { getTheme, useTheme } from '../lib/theme'

// ── Mermaid initialisation ────────────────────────────────────────────────────
// Sequence diagrams are a different renderer with its own defaults; without
// this they come out cramped and ignore the theme's spacing.
const SEQUENCE_CFG = {
  useMaxWidth:    true,
  showSequenceNumbers: true,
  actorMargin:    40,
  boxMargin:      10,
  mirrorActors:   false,
  wrap:           true,
}

const DARK_VARS = {
  background:        '#0d1117',
  mainBkg:           '#161b22',
  nodeBorder:        '#30363d',
  clusterBkg:        '#161b22',
  titleColor:        '#e6edf3',
  edgeLabelBackground: '#161b22',
  lineColor:         '#8b949e',
  actorBkg:          '#1e3a5f',
  actorBorder:       '#3b82f6',
  actorTextColor:    '#bfdbfe',
  actorLineColor:    '#8b949e',
  signalColor:       '#e6edf3',
  signalTextColor:   '#e6edf3',
  labelBoxBkgColor:  '#161b22',
  labelTextColor:    '#e6edf3',
  noteBkgColor:      '#451a03',
  noteTextColor:     '#fde68a',
  noteBorderColor:   '#f59e0b',
  sequenceNumberColor: '#0d1117',
}

const LIGHT_VARS = {
  background:          '#fbfcfe',
  primaryColor:        '#f3f5fb',
  primaryTextColor:    '#1b1f2e',
  primaryBorderColor:  '#c3cadb',
  mainBkg:             '#f3f5fb',
  nodeBorder:          '#c3cadb',
  clusterBkg:          '#f7f8fc',
  clusterBorder:       '#d5dae6',
  titleColor:          '#1b1f2e',
  textColor:           '#1b1f2e',
  edgeLabelBackground: '#ffffff',
  lineColor:           '#6b7389',
  actorBkg:            '#e8eefc',
  actorBorder:         '#4767e6',
  actorTextColor:      '#1b2b5c',
  actorLineColor:      '#9aa2b6',
  signalColor:         '#3a4057',
  signalTextColor:     '#1b1f2e',
  labelBoxBkgColor:    '#f3f5fb',
  labelBoxBorderColor: '#c3cadb',
  labelTextColor:      '#1b1f2e',
  noteBkgColor:        '#fff6e0',
  noteTextColor:       '#6b4500',
  noteBorderColor:     '#e0a640',
  sequenceNumberColor: '#ffffff',
  fontFamily:          'Inter, system-ui, sans-serif',
}

function initMermaid(theme) {
  const light = theme === 'light'
  mermaid.initialize({
    startOnLoad:  false,
    theme:        light ? 'base' : 'dark',
    darkMode:     !light,
    flowchart:    { curve: 'basis', useMaxWidth: true },
    sequence:     SEQUENCE_CFG,
    themeVariables: light ? LIGHT_VARS : DARK_VARS,
  })
}
initMermaid(getTheme())

// The backend compiles its node colours into the diagram source as classDefs
// tuned for a dark canvas. In light mode they are swapped for pale fills with
// the same hue, so the grouping still reads; the dark source is left as-is.
const LIGHT_TONES = {
  toneNeutral: 'fill:#f4f6fa,stroke:#a9b1c4,stroke-width:1.5px,color:#1b1f2e',
  toneBlue:    'fill:#e8f0fe,stroke:#3b82f6,stroke-width:1.5px,color:#1e3a8a',
  toneAmber:   'fill:#fff4e0,stroke:#d97706,stroke-width:1.5px,color:#7c3d00',
  toneMint:    'fill:#e7f7ee,stroke:#16a34a,stroke-width:1.5px,color:#14532d',
  toneRose:    'fill:#fde8ec,stroke:#e11d48,stroke-width:1.5px,color:#881337',
  toneIndigo:  'fill:#eef0ff,stroke:#6366f1,stroke-width:1.5px,color:#312e81',
  toneTeal:    'fill:#e2f7f4,stroke:#0d9488,stroke-width:1.5px,color:#134e4a',
}
function themedSource(code, theme) {
  if (theme !== 'light') return code
  return code.replace(/^(\s*classDef\s+)(\w+)\s+[^\n]*$/gm,
    (line, head, name) => LIGHT_TONES[name] ? `${head}${name} ${LIGHT_TONES[name]}` : line)
}

// ── Diagram kinds ─────────────────────────────────────────────────────────────
// Mirrors DIAGRAM_KINDS in backend/architect.py. The backend is still the
// authority — /api/architect/kinds is fetched on mount and replaces this list
// if they ever drift.
const DEFAULT_TABS = [
  { id: 'architecture', label: 'Architecture', icon: '🏗',
    blurb: 'Subsystems, their responsibilities and the boundaries between them.' },
  { id: 'sequence',     label: 'Sequence',     icon: '⇄',
    blurb: 'One end-to-end runtime flow in order, as a timeline of calls and replies.' },
  { id: 'dataflow',     label: 'Data flow',    icon: '🔀',
    blurb: 'Where data enters, how it is transformed, where it rests and where it leaves.' },
]
const TAB_ICON = { architecture: '🏗', sequence: '⇄', dataflow: '🔀' }

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
  const theme = useTheme()

  useEffect(() => {
    if (!code || !containerRef.current) return
    setError('')
    initMermaid(theme)
    const id = `mermaid-${Date.now()}`
    mermaid.render(id, themedSource(code, theme))
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
  }, [code, theme])

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
          padding: '10px 14px', background: 'var(--err-bg, #2d1b1b)',
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
          background: 'var(--graph-bg)',
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

function ExplanationAccordion({ text, label = 'Architecture' }) {
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
        <span style={{ fontWeight: 500 }}>{label} explanation (LLM)</span>
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

// Each kind has a different graph shape, so counting "nodes/edges/groups"
// would read 0/0/0 for a sequence diagram.
function statsFor(kind, graph) {
  if (!graph) return []
  if (kind === 'sequence') return [
    { label: 'Participants', value: (graph.participants || []).length },
    { label: 'Steps',        value: (graph.messages     || []).length },
  ]
  if (kind === 'dataflow') return [
    { label: 'Nodes',  value: (graph.nodes || []).length },
    { label: 'Flows',  value: (graph.flows || []).length },
    { label: 'Stores', value: (graph.nodes || []).filter(n => n.kind === 'store').length },
  ]
  return [
    { label: 'Nodes',  value: (graph.nodes  || []).length },
    { label: 'Edges',  value: (graph.edges  || []).length },
    { label: 'Groups', value: (graph.groups || []).length },
  ]
}

function GraphStats({ graph, kind }) {
  const items = statsFor(kind, graph)
  if (!items.length) return null
  return (
    <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
      {items.map(({ label, value }) => (
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

// A DFD's meaning lives in its shapes, and Mermaid draws no key. Rendered in
// HTML rather than inside the diagram so it cannot break the Mermaid parse.
function DataflowLegend() {
  const light = useTheme() === 'light'
  const items = [
    { shape: 'rect', tone: light ? '#d97706' : '#f59e0b', bg: light ? '#fff4e0' : '#451a03', label: 'External', hint: 'source or sink outside the system' },
    { shape: 'pill', tone: '#3b82f6', bg: light ? '#e8f0fe' : '#1e3a5f', label: 'Process',  hint: 'code that transforms data' },
    { shape: 'cyl',  tone: light ? '#16a34a' : '#22c55e', bg: light ? '#e7f7ee' : '#052e16', label: 'Store',    hint: 'where data comes to rest' },
  ]
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center',
      padding: '8px 12px', marginBottom: 12,
      background: 'var(--bg2)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius)', fontSize: 11, color: 'var(--txt2)',
    }}>
      <span style={{ color: 'var(--txt3)', textTransform: 'uppercase', letterSpacing: '0.06em', fontSize: 10 }}>
        Shapes
      </span>
      {items.map(({ shape, tone, bg, label, hint }) => (
        <span key={label} title={hint} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            width: 20, height: 12, background: bg, border: `1.5px solid ${tone}`,
            borderRadius: shape === 'pill' ? 6 : shape === 'cyl' ? '6px / 3px' : 2,
            flexShrink: 0,
          }} />
          <strong style={{ color: 'var(--txt)', fontWeight: 600 }}>{label}</strong>
          <span style={{ color: 'var(--txt3)' }}>— {hint}</span>
        </span>
      ))}
      <span style={{ marginLeft: 'auto', color: 'var(--txt3)' }}>
        Arrow labels name the data, not the call.
      </span>
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
  const [kind,       setKind]       = useState('architecture')
  const [tabs,       setTabs]       = useState(DEFAULT_TABS)
  // One result per kind, so switching tabs shows what you already generated
  // instead of throwing it away and charging another LLM round trip.
  const [byKind,     setByKind]     = useState(() =>
    architectResult ? { [architectResult.kind || 'architecture']: architectResult } : {})
  const stopRef = useRef(null)

  const result = byKind[kind] || null

  // A restored history entry arrives as a single flat result; file it under
  // its kind so the tab it belongs to shows it.
  useEffect(() => {
    if (!architectResult) return
    const k = architectResult.kind || 'architecture'
    setByKind(prev => (prev[k] === architectResult ? prev : { ...prev, [k]: architectResult }))
  }, [architectResult])

  // The backend owns the list of kinds; never offer one it cannot draw.
  useEffect(() => {
    let alive = true
    getArchitectKinds()
      .then(data => {
        if (!alive || !data?.kinds?.length) return
        setTabs(data.kinds.map(k => ({
          id: k.id, label: k.label, icon: TAB_ICON[k.id] || '◆', blurb: k.focus,
        })))
      })
      .catch(() => {})   // offline or old backend → keep DEFAULT_TABS
    return () => { alive = false }
  }, [])

  const addFeed = useCallback((msg, stageKey, type = 'status') => {
    setFeedEvents(prev => [...prev, { message: msg, stage: stageKey, type }])
  }, [])

  const handleGenerate = useCallback((targetKind = kind) => {
    if (!selectedRepo) return
    setRunning(true)
    setError('')
    setFeedEvents([])
    setStage('fetching')
    setKind(targetKind)
    // Clear only this tab — the other diagrams stay on screen.
    setByKind(prev => ({ ...prev, [targetKind]: null }))

    const stop = streamArchitect(selectedRepo.full_name, (ev) => {
      if (ev.type === 'status') {
        setStage(ev.stage)
        addFeed(ev.message, ev.stage)
      } else if (ev.type === 'explanation') {
        addFeed('Explanation generated.', 'explanation')
      } else if (ev.type === 'graph') {
        addFeed('Graph built.', 'graph')
      } else if (ev.type === 'done') {
        setStage('done')
        addFeed('Diagram ready!', 'done')
        const built = {
          kind:        ev.kind || targetKind,
          kindLabel:   ev.kind_label || '',
          mermaid:     ev.mermaid,
          explanation: ev.explanation,
          graph:       ev.graph,
          branch:      ev.branch,
          tokens:      ev.tokens,
          model:       ev.model || '',
        }
        setByKind(prev => ({ ...prev, [built.kind]: built }))
        setArchitectResult(built)
        unlock('execute')
        setRunning(false)
      } else if (ev.type === 'error') {
        setError(ev.message)
        setStage('error')
        addFeed(ev.message, 'error', 'error')
        setRunning(false)
      }
    }, targetKind)

    stopRef.current = stop
  }, [selectedRepo, kind, addFeed, setArchitectResult, unlock])

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
            Repository Diagrams
          </h2>
          <p style={{ fontSize: 12, color: 'var(--txt2)', margin: '2px 0 0' }}>
            {selectedRepo.full_name} — AI-generated Mermaid diagrams
          </p>
        </div>
        <div style={{ flex: 1 }} />
        {Object.values(byKind).some(Boolean) && (
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

      {/* ── Diagram-kind tabs ────────────────────────────────────────── */}
      <div style={{
        display: 'flex', gap: 4, marginBottom: 16, padding: 3,
        background: 'var(--bg2)', borderRadius: 'var(--radius)',
        border: '1px solid var(--border)',
      }}>
        {tabs.map(t => {
          const active = kind === t.id
          const ready  = Boolean(byKind[t.id])
          return (
            <button
              key={t.id}
              onClick={() => !running && setKind(t.id)}
              disabled={running}
              title={t.blurb}
              style={{
                flex: 1, padding: '8px 6px', fontSize: 12, fontWeight: 500,
                border: 'none', borderRadius: 6,
                cursor: running ? 'default' : 'pointer',
                background: active ? 'var(--accent)' : 'transparent',
                color:      active ? 'white' : 'var(--txt3)',
                opacity:    running && !active ? 0.5 : 1,
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
                transition: 'background 0.15s',
              }}
            >
              <span aria-hidden="true">{t.icon}</span>
              {t.label}
              {ready && (
                <span title="Already generated" style={{
                  fontSize: 9, color: active ? 'white' : 'var(--green)',
                  border: `1px solid ${active ? 'rgba(var(--ink),0.5)' : 'var(--green)'}`,
                  borderRadius: 3, padding: '0 3px', fontWeight: 700,
                }}>✓</span>
              )}
            </button>
          )
        })}
      </div>

      {/* ── Generate button or progress ──────────────────────────────── */}
      {!result && !running && (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>
            {tabs.find(t => t.id === kind)?.icon || '🏗'}
          </div>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>
            Generate {tabs.find(t => t.id === kind)?.label || 'Architecture'} Diagram
          </div>
          <div style={{ fontSize: 12, color: 'var(--txt2)', marginBottom: 24, maxWidth: 440, margin: '0 auto 24px' }}>
            RepoSage fetches the file tree from GitHub, asks the LLM to explain{' '}
            {tabs.find(t => t.id === kind)?.blurb?.replace(/\.$/, '') || 'the architecture'},
            then compiles it into an interactive Mermaid diagram.
          </div>
          {error && (
            <div style={{
              background: 'var(--err-bg, #2d1b1b)', border: '1px solid var(--red)',
              borderRadius: 'var(--radius)', padding: '10px 16px',
              color: 'var(--red)', fontSize: 12,
              marginBottom: 20, maxWidth: 480, margin: '0 auto 20px',
            }}>
              {error}
            </div>
          )}
          <button
            onClick={() => handleGenerate(kind)}
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
      {result && !running && (
        <div className="fade-in">
          {/* Stats row */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 12 }}>
            <GraphStats graph={result.graph} kind={kind} />
            <div style={{ flex: 1 }} />
            {result.model && (
              <span
                title={`Drawn by ${result.model}. Keys and models are tried in order, so a quota-exhausted primary falls back to a smaller model.`}
                style={{
                  fontSize: 10, color: 'var(--txt3)', padding: '2px 7px',
                  border: '1px solid var(--border2)', borderRadius: 4,
                  fontFamily: 'var(--mono, monospace)',
                }}
              >
                {result.model.split('/').pop()}
              </span>
            )}
            {result.tokens > 0 && (
              <span style={{ fontSize: 11, color: 'var(--txt3)' }}>
                {result.tokens.toLocaleString()} tokens used
              </span>
            )}
            <button
              onClick={() => handleGenerate(kind)}
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

          {kind === 'dataflow' && <DataflowLegend />}

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
                background: 'var(--accent-dim)', border: '1px solid color-mix(in srgb, var(--accent) 30%, transparent)',
                borderRadius: 3, padding: '1px 7px', color: 'var(--accent)',
              }}>
                {kind === 'sequence' ? 'Mermaid Sequence' : 'Mermaid Flowchart'}
              </span>
              <span>
                {result.kindLabel || tabs.find(t => t.id === kind)?.label || 'Architecture'}
              </span>
              {result.branch && (
                <span style={{ color: 'var(--txt3)', fontWeight: 400 }}>
                  · branch: {result.branch}
                </span>
              )}
            </div>
            <MermaidDiagram code={result.mermaid} />
          </div>

          {/* Explanation */}
          {result.explanation && (
            <ExplanationAccordion
              text={result.explanation}
              label={result.kindLabel || tabs.find(t => t.id === kind)?.label || 'Architecture'}
            />
          )}
        </div>
      )}
    </div>
  )
}
