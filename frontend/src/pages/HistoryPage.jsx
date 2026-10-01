import { useState, useEffect } from 'react'
import { getHistory } from '../api'

const STATUS_COLOR = {
  completed:  { bg: 'rgba(52,211,153,0.12)',  border: 'rgba(52,211,153,0.4)',  text: '#34d399' },
  failed:     { bg: 'rgba(239,68,68,0.12)',   border: 'rgba(239,68,68,0.4)',   text: '#f87171' },
  executing:  { bg: 'rgba(251,191,36,0.12)',  border: 'rgba(251,191,36,0.4)',  text: '#fbbf24' },
  analyzing:  { bg: 'rgba(96,165,250,0.12)',  border: 'rgba(96,165,250,0.4)',  text: '#60a5fa' },
  searching:  { bg: 'rgba(167,139,250,0.12)', border: 'rgba(167,139,250,0.4)', text: '#a78bfa' },
  auditing:   { bg: 'rgba(251,146,60,0.12)',  border: 'rgba(251,146,60,0.4)',  text: '#fb923c' },
}

function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString('en', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function StatusBadge({ status }) {
  const s = status || 'searching'
  const c = STATUS_COLOR[s] || STATUS_COLOR.searching
  return (
    <span style={{
      fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.07em',
      background: c.bg, border: `1px solid ${c.border}`, color: c.text,
      borderRadius: 4, padding: '2px 7px',
    }}>
      {s}
    </span>
  )
}

export default function HistoryPage({ onRestore, onNew, go }) {
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState('')
  const [expanded, setExpanded] = useState(null)

  useEffect(() => {
    getHistory()
      .then(data  => { setHistory(data.history || []); setLoading(false) })
      .catch(err  => { setError(err.message); setLoading(false) })
  }, [])

  const handleRestore = (entry) => {
    // Rebuild app state from history entry
    onRestore({
      task:         entry.task,
      repos:        entry.repos || [],
      selectedRepo: entry.selected_repo || null,
      analysis:     entry.analysis   || null,
      execResult:   entry.execution  || null,
      testResult:   entry.audit      || null,
      jobId:        entry.job_id     || '',
      historyId:    entry._id,
    })
    // Navigate to the deepest available step
    if      (entry.audit     || entry.execution) go('output')
    else if (entry.analysis)                     go('execute')
    else if (entry.selected_repo)                go('analyze')
    else if ((entry.repos||[]).length)           go('select')
    else                                         go('search')
  }

  if (loading) return (
    <div style={{ padding: 40, textAlign: 'center', color: 'var(--txt2)' }}>
      <div className="pulse" style={{ fontSize: 28, marginBottom: 12 }}>📚</div>
      Loading history…
    </div>
  )

  if (error) return (
    <div style={{ padding: 24, color: 'var(--red)', fontSize: 13 }}>⚠ {error}</div>
  )

  return (
    <div style={{ maxWidth: 820, margin: '0 auto', padding: '40px 20px 28px' }} className="fade-in">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 22 }}>
        <h2 style={{ fontSize: 24, fontWeight: 600, margin: 0, letterSpacing: '-0.03em' }}>Work history</h2>
        <span style={{ fontSize: 12, color: 'var(--txt2)', background: 'var(--bg3)', border: '1px solid var(--border)', borderRadius: 10, padding: '1px 8px' }}>
          {history.length} / 20
        </span>
        <div style={{ flex: 1 }} />
        {/* New task button */}
        <button onClick={onNew} className="lp-btn lp-btn-primary" style={{ padding: '8px 18px' }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          New task
        </button>
      </div>

      {history.length === 0 && (
        <div style={{
          background: 'var(--bg2)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)', padding: '36px 24px', textAlign: 'center',
          color: 'var(--txt2)', fontSize: 13,
        }}>
          <div style={{ fontSize: 32, marginBottom: 10 }}>🔍</div>
          No history yet. Run your first task to see it here!
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {history.map((entry, idx) => {
          const isOpen = expanded === entry._id
          const repo   = entry.selected_repo || (entry.repos || [])[0]
          const exec   = entry.execution
          const anal   = entry.analysis
          const metrics = anal?.metrics || {}

          return (
            <div
              key={entry._id}
              style={{
                background: 'var(--bg2)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius-lg)', overflow: 'hidden',
                transition: 'border-color 0.15s',
              }}
              onMouseOver={e => e.currentTarget.style.borderColor = 'var(--border2)'}
              onMouseOut={e  => e.currentTarget.style.borderColor = 'var(--border)'}
            >
              {/* Header row */}
              <div
                onClick={() => setExpanded(isOpen ? null : entry._id)}
                style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '13px 16px', cursor: 'pointer' }}
              >
                {/* Index */}
                <span style={{ fontSize: 11, color: 'var(--txt3)', width: 22, textAlign: 'right', flexShrink: 0 }}>
                  #{history.length - idx}
                </span>

                {/* Task */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {entry.task}
                  </div>
                  {repo && (
                    <div style={{ fontSize: 11, color: 'var(--txt2)', marginTop: 2 }}>
                      📦 {repo.full_name}
                      {repo.stars && <span style={{ color: 'var(--txt3)' }}> · ★{repo.stars}</span>}
                    </div>
                  )}
                </div>

                {/* Steps completed */}
                <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                  <StepDot done={!!(entry.repos?.length)} label="S" title="Search" />
                  <StepDot done={!!entry.selected_repo}  label="R" title="Repo selected" />
                  <StepDot done={!!entry.analysis}       label="A" title="Analyzed" />
                  <StepDot done={!!entry.execution}      label="E" title="Executed" />
                  <StepDot done={!!entry.audit}          label="T" title="Audited" />
                </div>

                <StatusBadge status={entry.status} />
                <span style={{ fontSize: 11, color: 'var(--txt3)', flexShrink: 0 }}>{fmtDate(entry.created_at)}</span>
                <ChevronIcon open={isOpen} />
              </div>

              {/* Expanded detail */}
              {isOpen && (
                <div style={{ padding: '0 16px 16px', borderTop: '1px solid var(--border)' }}>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, paddingTop: 14 }}>

                    {/* Execution summary */}
                    {exec && (
                      <div style={{ background: 'var(--bg3)', borderRadius: 8, padding: '12px 14px' }}>
                        <Label>Execution result</Label>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                          <span style={{ fontSize: 16 }}>{exec.returncode === 0 ? '✅' : '❌'}</span>
                          <span style={{ fontSize: 13, fontWeight: 500 }}>
                            {exec.returncode === 0 ? 'Succeeded' : 'Failed'}
                          </span>
                          {exec.elapsed_s && (
                            <span style={{ fontSize: 11, color: 'var(--txt3)' }}>· {exec.elapsed_s.toFixed(1)}s</span>
                          )}
                        </div>
                        <p style={{ fontSize: 12, color: 'var(--txt2)', margin: 0, lineHeight: 1.5 }}>
                          {(exec.summary || '').slice(0, 160)}{exec.summary?.length > 160 ? '…' : ''}
                        </p>
                      </div>
                    )}

                    {/* Analysis metrics */}
                    {anal && (
                      <div style={{ background: 'var(--bg3)', borderRadius: 8, padding: '12px 14px' }}>
                        <Label>Analysis</Label>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 12px', fontSize: 12 }}>
                          {[
                            ['Modules', metrics.total_modules],
                            ['Classes', metrics.total_classes],
                            ['FCG edges', metrics.total_fcg_edges],
                            ['Avg score', metrics.avg_module_score],
                          ].map(([k, v]) => v != null && (
                            <div key={k} style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--txt2)' }}>
                              <span>{k}</span><span style={{ fontWeight: 500, color: 'var(--txt)' }}>{v}</span>
                            </div>
                          ))}
                        </div>
                        {anal.readme_summary && (
                          <p style={{ fontSize: 11, color: 'var(--txt3)', margin: '8px 0 0', lineHeight: 1.5 }}>
                            {anal.readme_summary.slice(0, 100)}…
                          </p>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Run script preview */}
                  {exec?.run_script && (
                    <div style={{ marginTop: 10 }}>
                      <Label>Run script</Label>
                      <pre style={{
                        fontFamily: 'var(--mono)', fontSize: 11, lineHeight: 1.6,
                        background: 'var(--bg3)', borderRadius: 6, padding: '10px 12px',
                        overflowX: 'auto', maxHeight: 120, margin: 0, color: 'var(--txt2)',
                        whiteSpace: 'pre-wrap',
                      }}>
                        {exec.run_script.slice(0, 600)}{exec.run_script.length > 600 ? '\n…' : ''}
                      </pre>
                    </div>
                  )}

                  {/* Audit summary */}
                  {entry.audit?.report && (
                    <div style={{ marginTop: 10 }}>
                      <Label>Audit report</Label>
                      <p style={{ fontSize: 12, color: 'var(--txt2)', margin: 0, lineHeight: 1.5 }}>
                        {entry.audit.report.slice(0, 200)}…
                      </p>
                    </div>
                  )}

                  {/* Actions */}
                  <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap', alignItems: 'center' }}>
                    <button
                      onClick={() => handleRestore(entry)}
                      style={{
                        padding: '7px 16px', background: 'var(--accent)',
                        color: 'white', border: 'none', borderRadius: 6,
                        fontSize: 12, fontWeight: 500, cursor: 'pointer',
                      }}
                    >
                      ↺ Restore workflow
                    </button>
                    {entry.execution && (
                      <button
                        onClick={() => { handleRestore(entry); go('output') }}
                        style={{
                          padding: '7px 16px', background: 'transparent',
                          color: 'var(--txt)', border: '1px solid var(--border2)',
                          borderRadius: 6, fontSize: 12, cursor: 'pointer',
                        }}
                      >
                        📊 View output
                      </button>
                    )}
                    {/* New task — always visible in every card */}
                    <button
                      onClick={onNew}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 5,
                        padding: '7px 16px', background: 'transparent',
                        color: 'var(--green)', border: '1px solid var(--green)',
                        borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer',
                        marginLeft: 'auto',
                      }}
                    >
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
                      </svg>
                      New task
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function StepDot({ done, label, title }) {
  return (
    <div
      title={title}
      style={{
        width: 20, height: 20, borderRadius: '50%',
        background: done ? 'var(--accent)' : 'var(--bg3)',
        border: `1px solid ${done ? 'var(--accent)' : 'var(--border)'}`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 9, fontWeight: 700, color: done ? 'white' : 'var(--txt3)',
      }}
    >
      {label}
    </div>
  )
}

function ChevronIcon({ open }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ color: 'var(--txt3)', transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s', flexShrink: 0 }}>
      <polyline points="6 9 12 15 18 9"/>
    </svg>
  )
}

function Label({ children }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--txt3)', marginBottom: 7 }}>
      {children}
    </div>
  )
}
