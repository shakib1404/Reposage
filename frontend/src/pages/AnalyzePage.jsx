import { useState, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { analyzeRepo } from '../api'
import GraphCanvas from '../components/GraphCanvas'
import ClusterView from '../components/ClusterView'
import ScoreBar from '../components/ScoreBar'
import TreeView from '../components/TreeView'

// ── Graph descriptions shown in the header badge ─────────────────────────────
const GRAPH_META = {
  hct: {
    label: 'HCT',
    title: 'Hierarchical Code Tree',
    color: 'var(--accent)',
    desc:  'Package → Module → Class → Method. Shows ownership and code organisation.',
  },
  fcg: {
    label: 'FCG',
    title: 'Function Call Graph',
    color: '#34d399',
    desc:  'Directed edges from caller to callee. Edge weight = call-site count. Reveals runtime coupling.',
  },
  mdg: {
    label: 'MDG',
    title: 'Module Dependency Graph',
    color: '#60a5fa',
    desc:  'Import-level edges between modules. In-degree ↑ = higher reuse. Used for PageRank scoring.',
  },
}

// ── Full-screen modal ─────────────────────────────────────────────────────────
function GraphModal({ graphKey, analysis, onClose }) {
  const { modules = [], classes = [], fcg_edges = [], mdg_edges = [] } = analysis
  const meta = GRAPH_META[graphKey]
  const [expandedClasses, setExpandedClasses] = useState(new Set())

  const toggleClass = (name) => {
    setExpandedClasses(prev => {
      const next = new Set(prev)
      next.has(name) ? next.delete(name) : next.add(name)
      return next
    })
  }

  // Close on Escape
  useEffect(() => {
    const handler = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const body = (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000,
      background: 'rgba(6,7,10,0.97)',
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Modal header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 12,
        padding: '14px 20px',
        borderBottom: '1px solid var(--border)',
        flexShrink: 0,
      }}>
        <span style={{
          fontSize: 11, fontWeight: 600, letterSpacing: '0.08em',
          textTransform: 'uppercase', color: meta.color,
          border: `1px solid ${meta.color}66`,
          borderRadius: 4, padding: '2px 7px',
        }}>
          {meta.label}
        </span>
        <span style={{ fontWeight: 600, fontSize: 15 }}>{meta.title}</span>
        <span style={{ color: 'var(--txt2)', fontSize: 12, flex: 1 }}>{meta.desc}</span>

        {graphKey === 'fcg' && (
          <span style={{ fontSize: 11, color: 'var(--txt3)' }}>
            {fcg_edges.length} edges · {[...new Set([...fcg_edges.map(e=>e.from),...fcg_edges.map(e=>e.to)])].length} nodes
          </span>
        )}
        {graphKey === 'mdg' && (
          <span style={{ fontSize: 11, color: 'var(--txt3)' }}>
            {mdg_edges.length} edges · {modules.length} modules
          </span>
        )}
        {graphKey === 'hct' && (
          <span style={{ fontSize: 11, color: 'var(--txt3)' }}>
            {modules.length} modules · {classes.length} classes
          </span>
        )}

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button
            onClick={onClose}
            style={{
              width: 28, height: 28, borderRadius: '50%',
              border: '1px solid var(--border2)',
              background: 'var(--bg3)', color: 'var(--txt)',
              cursor: 'pointer', fontSize: 14,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            ✕
          </button>
        </div>
      </div>

      {/* Modal content */}
      <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
        {graphKey === 'hct' && (
          <div style={{ height: '100%', padding: '16px 24px', overflowY: 'auto' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, height: '100%' }}>
              <div>
                <SectionLabel>Module tree</SectionLabel>
                <TreeView modules={modules} classes={classes} height="auto" fullscreen />
              </div>
              <div>
                <SectionLabel>Classes & methods</SectionLabel>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 2, overflowY: 'auto', maxHeight: 'calc(100vh - 160px)' }}>
                  {classes.slice(0, 30).map(c => (
                    <div key={c.name} style={{ marginBottom: 6 }}>
                      <div style={{ color: '#a78bfa', fontWeight: 500 }}>
                        {c.name}
                        {(c.bases||[]).filter(b => b && b !== 'object').length > 0 && (
                          <span style={{ color: 'var(--txt3)', fontWeight: 400, fontSize: 11 }}>
                            {' '}({c.bases.join(', ')})
                          </span>
                        )}
                        <span style={{ color: 'var(--txt3)', fontSize: 11, marginLeft: 8 }}>
                          in {c.module_short || c.module}
                        </span>
                      </div>
                      {c.docstring && (
                        <div style={{ color: 'var(--txt3)', fontSize: 11, fontStyle: 'italic', paddingLeft: 12 }}>
                          {c.docstring.slice(0, 100)}{c.docstring.length > 100 ? '…' : ''}
                        </div>
                      )}
                      <div style={{ paddingLeft: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {(expandedClasses.has(c.name)
                          ? (c.method_names || [])
                          : (c.method_names || []).slice(0, 8)
                        ).map(m => (
                          <span key={m} style={{ color: 'var(--green)', fontSize: 11, opacity: 0.8 }}>{m}()</span>
                        ))}
                        {(c.method_names||[]).length > 8 && (
                          <span
                            onClick={() => toggleClass(c.name)}
                            style={{
                              color: 'var(--accent)', fontSize: 11,
                              cursor: 'pointer', textDecoration: 'underline',
                              textDecorationStyle: 'dotted', userSelect: 'none',
                            }}
                          >
                            {expandedClasses.has(c.name)
                              ? '− collapse'
                              : `+${c.method_names.length - 8} more`}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {graphKey === 'fcg' && (
          <GraphCanvas
            edges={fcg_edges}
            nodes={classes.map(c => c.name)}
            height="100%"
            colorScheme="green"
            fullscreen
          />
        )}

        {graphKey === 'mdg' && (
          <GraphCanvas
            edges={mdg_edges}
            nodes={modules.map(m => m.short_name || m.name)}
            height="100%"
            colorScheme="blue"
            fullscreen
          />
        )}
      </div>
    </div>
  )

  return createPortal(body, document.body)
}

// ── Expandable card ───────────────────────────────────────────────────────────
function ExpandableCard({ graphKey, children }) {
  const [expanded, setExpanded] = useState(false)
  const meta = GRAPH_META[graphKey]

  return (
    <>
      <div style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', padding: '12px 14px',
      }}>
        {/* Card header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <span style={{
            fontSize: 10, fontWeight: 600, letterSpacing: '0.07em',
            textTransform: 'uppercase', color: meta.color,
            border: `1px solid ${meta.color}55`, borderRadius: 3, padding: '1px 6px',
            flexShrink: 0,
          }}>
            {meta.label}
          </span>
          <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--txt)', flex: 1 }}>
            {meta.title}
          </span>
          <button
            onClick={() => setExpanded(true)}
            title="Expand to full screen"
            style={{
              display: 'flex', alignItems: 'center', gap: 4,
              padding: '3px 10px', fontSize: 11,
              border: '1px solid var(--border2)', borderRadius: 4,
              background: 'transparent', color: 'var(--txt2)',
              cursor: 'pointer',
            }}
            onMouseOver={e => { e.currentTarget.style.borderColor = 'var(--accent)'; e.currentTarget.style.color = 'var(--accent)' }}
            onMouseOut={e  => { e.currentTarget.style.borderColor = 'var(--border2)'; e.currentTarget.style.color = 'var(--txt2)' }}
          >
            <ExpandIcon /> Expand
          </button>
        </div>
        {children}
        <div style={{ marginTop: 6, fontSize: 11, color: 'var(--txt3)', fontStyle: 'italic' }}>
          {meta.desc}
        </div>
      </div>

      {expanded && (
        <GraphModal graphKey={graphKey} analysis={window.__rm_analysis__} onClose={() => setExpanded(false)} />
      )}
    </>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function AnalyzePage({ task, selectedRepo, analysis, setAnalysis, unlock, go }) {
  const [loading,   setLoading]   = useState(!analysis)
  const [status,    setStatus]    = useState('Fetching README…')
  const [progress,  setProgress]  = useState(5)
  const [view,      setView]      = useState('overview')
  const [error,     setError]     = useState('')
  const [expandedGraph, setExpandedGraph] = useState(null) // 'hct'|'fcg'|'mdg'|null

  useEffect(() => {
    if (analysis) { setLoading(false); setProgress(100); return }
    run()
  }, [])

  // Stash analysis on window so ExpandableCard can access it in the portal
  useEffect(() => {
    if (analysis) window.__rm_analysis__ = analysis
  }, [analysis])

  const run = async () => {
    try {
      setStatus('Fetching README via Jina…');           setProgress(10)
      await delay(300)
      setStatus('Building HCT, FCG, MDG…');             setProgress(30)
      const data = await analyzeRepo(task, selectedRepo)
      setProgress(70)
      setStatus('Scoring modules (PageRank + cyclomatic)…'); setProgress(85)
      await delay(300)
      setStatus('Selecting core components…');           setProgress(95)
      await delay(200)
      setAnalysis(data)
      window.__rm_analysis__ = data
      unlock('execute')
      setProgress(100)
      setStatus('Analysis complete')
    } catch (e) {
      setError(e.message)
      setStatus('Error — using fallback data')
    } finally {
      setLoading(false)
    }
  }

  // Keyboard: Escape closes modal
  useEffect(() => {
    const h = e => { if (e.key === 'Escape') setExpandedGraph(null) }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  if (loading)   return <LoadingState status={status} progress={progress} repo={selectedRepo} />
  if (!analysis) return <div style={{ padding: 24, color: 'var(--red)' }}>Analysis failed: {error}</div>

  const {
    modules = [], classes = [], fcg_edges = [], mdg_edges = [],
    core_components = [], core_scores = [], metrics = {},
    task_plan = [], readme_summary = '', key_files = [],
  } = analysis

  return (
    <div style={{ padding: '18px 22px', maxWidth: 1200, margin: '0 auto' }} className="fade-in">

      {/* ── Toolbar ──────────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: 15, fontWeight: 600, margin: 0, letterSpacing: '-0.01em' }}>
            {selectedRepo?.full_name}
          </h2>
          {readme_summary && (
            <p style={{ fontSize: 12, color: 'var(--txt2)', margin: '2px 0 0', lineHeight: 1.4 }}>
              {readme_summary}
            </p>
          )}
        </div>
        <div style={{ flex: 1 }} />
        <TabBtn active={view === 'overview'} onClick={() => setView('overview')}>Overview</TabBtn>
        <TabBtn active={view === 'cluster'}  onClick={() => setView('cluster')}>Cluster</TabBtn>
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
      </div>

      {/* ── Metrics strip ────────────────────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(8,1fr)', gap: 8, marginBottom: 8 }}>
        {[
          { label: 'Modules',       value: metrics.total_modules    ?? modules.length },
          { label: 'Classes',       value: metrics.total_classes    ?? classes.length },
          { label: 'Methods',       value: metrics.total_methods    ?? '—' },
          { label: 'FCG edges',     value: metrics.total_fcg_edges  ?? fcg_edges.length },
          { label: 'MDG edges',     value: metrics.total_mdg_edges  ?? mdg_edges.length },
          { label: 'Avg score',     value: metrics.avg_module_score ?? '—' },
          { label: 'Avg CC',        value: metrics.avg_cyclomatic   ?? '—', title: 'Average cyclomatic complexity across all functions' },
          { label: 'Est. Bugs',     value: metrics.halstead_bugs != null ? metrics.halstead_bugs.toFixed(2) : '—', title: 'Halstead estimated bug count' },
        ].map(m => (
          <div key={m.label} title={m.title || ''} style={{
            background: 'var(--bg2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', padding: '8px 12px',
          }}>
            <div style={{ fontSize: 10, color: 'var(--txt2)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>{m.label}</div>
            <div style={{ fontSize: 18, fontWeight: 600 }}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* ── Complexity sub-strip ─────────────────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 8, marginBottom: 14 }}>
        {[
          { label: 'Max CC',             value: metrics.max_cyclomatic  ?? '—', title: 'Highest cyclomatic complexity (single function)' },
          { label: 'Total lines',        value: metrics.total_lines?.toLocaleString() ?? '—' },
          { label: 'Halstead volume',    value: metrics.halstead_volume != null ? Math.round(metrics.halstead_volume).toLocaleString() : '—', title: 'Sum of Halstead volume across all functions' },
          { label: 'Halstead effort',    value: metrics.halstead_effort != null ? Math.round(metrics.halstead_effort).toLocaleString() : '—', title: 'Sum of Halstead effort — proxy for implementation cost' },
        ].map(m => (
          <div key={m.label} title={m.title || ''} style={{
            background: 'var(--bg2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', padding: '8px 12px',
          }}>
            <div style={{ fontSize: 10, color: 'var(--txt2)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>{m.label}</div>
            <div style={{ fontSize: 18, fontWeight: 600 }}>{m.value}</div>
          </div>
        ))}
      </div>

      {view === 'cluster' ? (
        <ClusterView modules={modules} fcgEdges={fcg_edges} coreComponents={core_components} />
      ) : (
        <>
          {/* ── Row 1: HCT + Scores ──────────────────────────────────────── */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>

            {/* HCT */}
            <ExpandableCard graphKey="hct">
              <TreeView modules={modules} classes={classes} height={220} />
            </ExpandableCard>

            {/* Module scores */}
            <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '12px 14px' }}>
              <SectionLabel>Module importance scores</SectionLabel>
              {modules.slice(0, 10).map((m, i) => (
                <ScoreBar
                  key={m.name}
                  name={m.name}
                  score={typeof m.score === 'number' ? m.score : 8 - i * 0.4}
                  index={i}
                />
              ))}
              {metrics.most_depended_on && (
                <div style={{ marginTop: 8, fontSize: 11, color: 'var(--txt3)' }}>
                  Most depended-on: <span style={{ color: 'var(--accent)' }}>{metrics.most_depended_on}</span>
                </div>
              )}
            </div>
          </div>

          {/* ── Row 2: FCG + MDG ─────────────────────────────────────────── */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
            <ExpandableCard graphKey="fcg">
              <GraphCanvas
                edges={fcg_edges}
                nodes={classes.map(c => c.name)}
                height={220}
                colorScheme="green"
              />
            </ExpandableCard>

            <ExpandableCard graphKey="mdg">
              <GraphCanvas
                edges={mdg_edges}
                nodes={modules.map(m => m.short_name || m.name)}
                height={220}
                colorScheme="blue"
              />
            </ExpandableCard>
          </div>

          {/* ── Row 3: Complexity Analysis ───────────────────────────────── */}
          <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '12px 14px', marginBottom: 12 }}>
            <SectionLabel>Cyclomatic &amp; Halstead complexity — per module</SectionLabel>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 6 }}>
              {modules.slice(0, 12).map(m => {
                const cc   = typeof m.avg_cc === 'number' ? m.avg_cc : 1
                const rank = m.cc_rank || 'A'
                const rankColor = { A: '#22c55e', B: '#86efac', C: '#facc15', D: '#fb923c', E: '#ef4444', F: '#dc2626' }[rank] || '#94a3b8'
                const bugs = typeof m.halstead_bugs === 'number' ? m.halstead_bugs.toFixed(3) : '—'
                const vol  = typeof m.halstead_volume === 'number' ? Math.round(m.halstead_volume) : 0
                return (
                  <div key={m.name} style={{ background: 'var(--bg3)', borderRadius: 'var(--radius)', padding: '7px 10px', display: 'flex', flexDirection: 'column', gap: 3 }}>
                    <div style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--accent)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={m.path}>{m.short_name || m.name}</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontSize: 10, fontWeight: 700, color: rankColor, border: `1px solid ${rankColor}`, borderRadius: 3, padding: '0 4px', lineHeight: '16px' }}>{rank}</span>
                      <span style={{ fontSize: 11, color: 'var(--txt2)' }}>CC {cc}</span>
                      <span style={{ fontSize: 11, color: 'var(--txt3)', marginLeft: 'auto' }}>vol {vol.toLocaleString()}</span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <div style={{ flex: 1, height: 3, background: 'var(--bg2)', borderRadius: 2, overflow: 'hidden' }}>
                        <div style={{ height: '100%', width: Math.min(100, (cc / 15) * 100) + '%', background: rankColor, borderRadius: 2 }} />
                      </div>
                      <span style={{ fontSize: 10, color: 'var(--txt3)' }}>bugs≈{bugs}</span>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          {/* ── Row 4: Core components + task plan ───────────────────────── */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>

            {/* Core components */}
            <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '12px 14px' }}>
              <SectionLabel>Core components — ranked by J(c)</SectionLabel>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 }}>
                {core_components.map((c, i) => (
                  <div key={c} style={{
                    padding: '5px 12px',
                    background: 'var(--accent-dim)',
                    border: '1px solid rgba(93,142,255,0.25)',
                    borderRadius: 20, fontSize: 12, fontWeight: 500,
                    display: 'flex', alignItems: 'center', gap: 6,
                  }}>
                    <span style={{ color: 'var(--txt2)', fontSize: 10 }}>#{i + 1}</span>
                    <span style={{ color: 'var(--accent)' }}>{c}</span>
                    {core_scores[i] != null && (
                      <span style={{ color: 'var(--txt2)', fontSize: 10 }}>
                        · {Number(core_scores[i]).toFixed(1)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
              {metrics.most_called_class && (
                <div style={{ fontSize: 11, color: 'var(--txt3)' }}>
                  Most-called class: <span style={{ color: '#a78bfa' }}>{metrics.most_called_class}</span>
                  {' '}· Total lines: <span style={{ color: 'var(--txt)' }}>{metrics.total_lines?.toLocaleString()}</span>
                </div>
              )}
            </div>

            {/* Task plan */}
            <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '12px 14px' }}>
              <SectionLabel>Task execution plan</SectionLabel>
              {task_plan.length > 0 ? (
                <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: 'var(--txt2)', lineHeight: 1.8 }}>
                  {task_plan.map((step, i) => (
                    <li key={i} style={{ marginBottom: 2 }}>{step}</li>
                  ))}
                </ol>
              ) : (
                <div style={{ fontSize: 12, color: 'var(--txt3)' }}>No task plan available</div>
              )}
              {key_files.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 10, color: 'var(--txt3)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>
                    Key files
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                    {key_files.slice(0, 6).map(f => (
                      <span key={f} style={{
                        fontSize: 10, fontFamily: 'var(--mono)',
                        background: 'var(--bg3)', border: '1px solid var(--border)',
                        borderRadius: 3, padding: '1px 6px', color: 'var(--accent)',
                      }}>
                        {f}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// ── Small reusables ───────────────────────────────────────────────────────────
function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 500, color: 'var(--txt2)',
      textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10,
    }}>
      {children}
    </div>
  )
}

function TabBtn({ children, active, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 14px',
        border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`,
        borderRadius: 'var(--radius)',
        background: active ? 'var(--accent-dim)' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--txt2)',
        fontSize: 12, fontWeight: 500, cursor: 'pointer',
      }}
    >
      {children}
    </button>
  )
}

function ExpandIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 3 21 3 21 9"/>
      <polyline points="9 21 3 21 3 15"/>
      <line x1="21" y1="3" x2="14" y2="10"/>
      <line x1="3" y1="21" x2="10" y2="14"/>
    </svg>
  )
}

function LoadingState({ status, progress, repo }) {
  return (
    <div style={{ maxWidth: 480, margin: '60px auto', padding: 24, textAlign: 'center' }}>
      <div className="pulse" style={{ fontSize: 36, marginBottom: 16 }}>🔬</div>
      <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 6 }}>Analysing {repo?.name}</div>
      <div style={{ color: 'var(--txt2)', fontSize: 13, marginBottom: 20 }}>{status}</div>
      <div style={{ height: 4, background: 'var(--bg3)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%', width: progress + '%',
          background: 'var(--accent)', borderRadius: 2, transition: 'width 0.5s ease',
        }} />
      </div>
      <div style={{ marginTop: 12, fontSize: 12, color: 'var(--txt3)' }}>{progress}%</div>
    </div>
  )
}

const delay = ms => new Promise(r => setTimeout(r, ms))
