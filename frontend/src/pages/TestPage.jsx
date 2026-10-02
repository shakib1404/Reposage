import { useState, useRef, useEffect } from 'react'
import { streamTest } from '../api'
import { downloadAuditReport } from '../lib/auditPdf'
import { SCANNERS, RULE_INFO } from '../lib/auditRules'
import CodeSnippet from '../components/CodeSnippet'

// Scanner metadata lives in lib/auditRules.js so the screen and the PDF
// cannot describe the same scanner differently — and so adding a tenth one is
// a single edit rather than two lists to keep in step.

// ── Severity config ───────────────────────────────────────────────────────────
const SEV = {
  critical: { color: '#f87171', bg: 'rgba(248,113,113,0.12)', label: 'Critical' },
  high:     { color: '#fb923c', bg: 'rgba(251,146,60,0.12)',  label: 'High'     },
  medium:   { color: '#fbbf24', bg: 'rgba(251,191,36,0.12)',  label: 'Medium'   },
  low:      { color: '#34d399', bg: 'rgba(52,211,153,0.12)',  label: 'Low'      },
  info:     { color: '#8888a0', bg: 'rgba(136,136,160,0.12)', label: 'Info'     },
}

const GRADE_STYLE = {
  A: { color: '#34d399', bg: 'rgba(52,211,153,0.15)',   border: '#34d399' },
  B: { color: '#5d8eff', bg: 'rgba(93,142,255,0.15)',   border: '#5d8eff' },
  C: { color: '#fbbf24', bg: 'rgba(251,191,36,0.15)',   border: '#fbbf24' },
  D: { color: '#fb923c', bg: 'rgba(251,146,60,0.15)',   border: '#fb923c' },
  F: { color: '#f87171', bg: 'rgba(248,113,113,0.15)',  border: '#f87171' },
}

// Where in the repo a finding lives, as the backend classifies it.
const AREA_LABEL = {
  source: 'source', test: 'tests', docs: 'docs', example: 'examples',
}


// Per-finding weight = midpoint of the finding's CVSS v3.1 qualitative
// severity band (FIRST.org / NIST NVD standard: None 0.0, Low 0.1-3.9,
// Medium 4.0-6.9, High 7.0-8.9, Critical 9.0-10.0). Must match backend
// tester.py's CVSS_WEIGHTS — used here only to *display* each severity's
// share of risk exposure; the score itself always comes from the backend.
const CVSS_WEIGHTS = { critical: 9.5, high: 8.0, medium: 5.5, low: 2.0, info: 0.0 }

// ─────────────────────────────────────────────────────────────────────────────

export default function TestPage({ selectedRepo, jobId, setTestResult, unlock, go }) {
  const [phase, setPhase]           = useState('idle')   // idle | running | done | error
  const [scanStatus, setScanStatus] = useState({})       // {id: 'pending'|'running'|'done'|'error'}
  const [scanCounts, setScanCounts] = useState({})       // {id: n}
  const [log, setLog]               = useState([])       // [{text, type}]
  const [report, setReport]         = useState(null)     // final report object
  const [activeTab, setActiveTab]   = useState('findings')  // findings | scanner | raw
  const [sevFilter, setSevFilter]   = useState('all')
  const [scanFilter, setScanFilter] = useState('all')
  const [expandedFinding, setExpandedFinding] = useState(null)
  const cleanupRef = useRef(null)
  const logEndRef  = useRef(null)

  function downloadReport() {
    if (report) downloadAuditReport(report)
  }

  useEffect(() => {
    // Auto-scroll log
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  function startAudit() {
    if (!selectedRepo) return
    setPhase('running')
    setLog([])
    setReport(null)
    setScanStatus({})
    setScanCounts({})

    const cleanup = streamTest(selectedRepo.full_name, jobId || '', (ev) => {
      if (ev.type === 'error') {
        setPhase('error')
        addLog(ev.body || 'Unknown error', 'error')
        return
      }

      if (ev.type === 'status') {
        addLog(ev.title + (ev.body ? ` — ${ev.body}` : ''), 'status')
        return
      }

      if (ev.type === 'warning') {
        addLog('⚠ ' + ev.title + ': ' + ev.body, 'warning')
        return
      }

      if (ev.type === 'scanner_start') {
        setScanStatus(p => ({ ...p, [ev.scanner]: 'running' }))
        addLog(`${ev.icon} ${ev.title}: running…`, 'scan')
        return
      }

      if (ev.type === 'scanner_done') {
        setScanStatus(p => ({ ...p, [ev.scanner]: 'done' }))
        // ev.count is authoritative; ev.findings is truncated for payload size.
        setScanCounts(p => ({ ...p, [ev.scanner]: ev.count ?? (ev.findings || []).length }))
        addLog(`${ev.icon} ${ev.title}: ${ev.body}`, 'done')
        return
      }

      if (ev.type === 'scanner_error') {
        setScanStatus(p => ({ ...p, [ev.scanner]: 'error' }))
        addLog(`${ev.icon} ${ev.title}: ⚠ ${ev.body}`, 'error')
        return
      }

      if (ev.type === 'done' && ev.report) {
        setReport(ev.report)
        setPhase('done')
        setTestResult(ev.report)
        unlock('test')
        addLog('✓ Audit complete — ' + ev.title, 'done')
      }
    })

    cleanupRef.current = cleanup
  }

  function stopAudit() {
    cleanupRef.current?.()
    setPhase('idle')
    addLog('Audit stopped by user.', 'warning')
  }

  function addLog(text, type = 'status') {
    setLog(prev => [...prev, { text, type }])
  }

  // ── Findings helpers ────────────────────────────────────────────────────────

  const findings = report?.findings || []

  // Worst first, and the findings the grade was computed from above the ones
  // it wasn't. Unsorted, the list opened on whatever the first scanner
  // happened to emit — for psf/requests that was eight `info` nits from
  // docs/conf.py, burying every real issue below the fold.
  const SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 }
  const filtered = findings
    .filter(f => {
      if (sevFilter !== 'all' && f.severity !== sevFilter) return false
      if (scanFilter !== 'all' && f.scanner !== scanFilter) return false
      return true
    })
    .slice()
    .sort((a, b) => {
      const ga = (a.area || 'source') === 'source' ? 0 : 1
      const gb = (b.area || 'source') === 'source' ? 0 : 1
      if (ga !== gb) return ga - gb
      return (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9)
    })

  const sevCounts = report?.severity || {}
  const totalFindings = report?.total || 0

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 97px)', overflow: 'hidden' }}>

      {/* ── Left panel: scanner status + log ──────────────────────────────── */}
      <div style={{ width: 280, flexShrink: 0, borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>

        {/* Header */}
        <div style={{ padding: '16px 16px 12px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            Code Audit
          </div>
          <div style={{ fontSize: 11, color: 'var(--txt2)', marginBottom: 12 }}>
            {selectedRepo?.full_name || 'No repo selected'}
          </div>

          {phase === 'idle' || phase === 'error' ? (
            <button
              onClick={startAudit}
              disabled={!selectedRepo}
              style={{ width: '100%', padding: '9px 0', background: 'var(--accent)', border: 'none', borderRadius: 'var(--radius)', color: 'white', fontSize: 13, fontWeight: 500, cursor: selectedRepo ? 'pointer' : 'not-allowed', opacity: selectedRepo ? 1 : 0.5 }}
            >
              {phase === 'error' ? 'Retry Audit' : '▶  Start Audit'}
            </button>
          ) : phase === 'running' ? (
            <button
              onClick={stopAudit}
              style={{ width: '100%', padding: '9px 0', background: 'transparent', border: '1px solid var(--red)', borderRadius: 'var(--radius)', color: 'var(--red)', fontSize: 13, fontWeight: 500, cursor: 'pointer' }}
            >
              ■  Stop
            </button>
          ) : (
            <>
              <button
                onClick={startAudit}
                style={{ width: '100%', padding: '9px 0', background: 'transparent', border: '1px solid var(--border2)', borderRadius: 'var(--radius)', color: 'var(--txt)', fontSize: 13, cursor: 'pointer', marginBottom: 6 }}
              >
                ↺  Re-run Audit
              </button>
              {report && (
                <button
                  onClick={downloadReport}
                  title="Full report: executive summary, methodology, scoring, scanner coverage, priority findings, a linked issue index and an appendix listing every occurrence"
                  style={{ width: '100%', padding: '9px 0', background: 'var(--green-dim)', border: '1px solid var(--green)', borderRadius: 'var(--radius)', color: 'var(--green)', fontSize: 13, fontWeight: 500, cursor: 'pointer' }}
                >
                  ↓ Download Full Report (PDF)
                </button>
              )}
            </>
          )}
        </div>

        {/* Scanner cards */}
        <div style={{ padding: '10px 12px 0', overflowY: 'auto', flex: 1 }}>
          {/* Pipeline label */}
          <div style={{ fontSize: 10, fontWeight: 500, color: 'var(--txt3)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
            Scanners
          </div>

          {SCANNERS.map((sc, i) => {
            const status = scanStatus[sc.id] || 'pending'
            const count  = scanCounts[sc.id] ?? null
            return (
              <div key={sc.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, padding: '7px 8px', borderRadius: 8, background: status === 'running' ? 'var(--accent-dim)' : 'transparent', border: `1px solid ${status === 'running' ? 'var(--accent)' : 'var(--border)'}`, transition: 'all 0.2s' }}>
                {/* Connector line */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0 }}>
                  <div style={{ width: 20, height: 20, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, fontSize: 12,
                    background: status === 'done'    ? 'var(--green-dim)' :
                                status === 'running' ? 'var(--accent-dim)' :
                                status === 'error'   ? 'var(--red-dim)' : 'var(--bg3)',
                    border: `1.5px solid ${
                                status === 'done'    ? 'var(--green)'  :
                                status === 'running' ? 'var(--accent)' :
                                status === 'error'   ? 'var(--red)'    : 'var(--border2)'}`,
                  }}>
                    {status === 'done'    ? '✓' :
                     status === 'running' ? <span className="spin" style={{ fontSize: 10 }}>◌</span> :
                     status === 'error'   ? '✕' :
                     <span style={{ color: 'var(--txt3)', fontSize: 9 }}>{i + 1}</span>}
                  </div>
                </div>

                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500, display: 'flex', alignItems: 'center', gap: 5 }}>
                    <span>{sc.icon}</span>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sc.name}</span>
                    {/* The sidebar is what stays on screen once the audit has
                        finished, so the marker has to be here too — not only on
                        the progress cards, which disappear with the run. */}
                    {sc.own && (
                      <span title="Built into RepoSage — not a wrapper around another tool"
                        style={{ fontSize: 8, fontWeight: 700, letterSpacing: '0.04em', color: 'var(--accent)', border: '1px solid var(--accent)', borderRadius: 3, padding: '0 3px', flexShrink: 0 }}>
                        OWN
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--txt3)', marginTop: 1 }}>{sc.tool}</div>
                </div>

                {count !== null && (
                  <span style={{ fontSize: 10, fontWeight: 600, color: count > 0 ? 'var(--yellow)' : 'var(--green)', flexShrink: 0 }}>
                    {count}
                  </span>
                )}
                {status === 'running' && count === null && (
                  <span className="pulse" style={{ fontSize: 10, color: 'var(--accent)' }}>●</span>
                )}
              </div>
            )
          })}

          {/* Live log */}
          <div style={{ fontSize: 10, fontWeight: 500, color: 'var(--txt3)', marginTop: 12, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
            Live log
          </div>
          <div style={{ fontSize: 11, fontFamily: 'var(--mono)', lineHeight: 1.6, paddingBottom: 16 }}>
            {log.map((entry, i) => (
              <div key={i} style={{ color: entry.type === 'error' ? 'var(--red)' : entry.type === 'done' ? 'var(--green)' : entry.type === 'warning' ? 'var(--yellow)' : 'var(--txt2)', marginBottom: 3 }}>
                {entry.text}
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>

      {/* ── Right panel: results ──────────────────────────────────────────── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>

        {/* Idle state */}
        {phase === 'idle' && !report && (
          <div className="fade-in" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 16, color: 'var(--txt3)', textAlign: 'center' }}>
            <div style={{ fontSize: 64 }}>🔬</div>
            <div style={{ fontSize: 18, fontWeight: 600, color: 'var(--txt2)' }}>Python Code Audit</div>
            <div style={{ maxWidth: 420, fontSize: 13, lineHeight: 1.7, color: 'var(--txt3)' }}>
              Runs {SCANNERS.length} analysers on <strong style={{ color: 'var(--txt2)' }}>{selectedRepo?.full_name || 'the selected repo'}</strong>:<br/>
              linting · security · CVEs · types · secrets · dead code · patterns · architecture
            </div>
            <div style={{
              maxWidth: 440, fontSize: 12, lineHeight: 1.65, color: 'var(--txt3)',
              background: 'var(--purple-dim)', border: '1px solid rgba(167,139,250,0.3)',
              borderRadius: 'var(--radius)', padding: '10px 14px', marginTop: 4,
            }}>
              <strong style={{ color: 'var(--purple)' }}>🏛️ Architecture Health</strong> is RepoSage's
              own analysis, not a third-party linter. It reads the dependency and
              call graphs to find import cycles, god modules and functions no
              entry point can reach — defects that live in the structure of the
              code, where a file-at-a-time tool cannot see them.
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center', marginTop: 8 }}>
              {SCANNERS.map(sc => (
                <span key={sc.id} style={{ fontSize: 11, padding: '4px 10px', background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 20, color: 'var(--txt2)' }}>
                  {sc.icon} {sc.name}
                </span>
              ))}
            </div>
            <button
              onClick={startAudit}
              disabled={!selectedRepo}
              style={{ marginTop: 8, padding: '10px 28px', background: 'var(--accent)', border: 'none', borderRadius: 'var(--radius)', color: 'white', fontSize: 14, fontWeight: 500, cursor: selectedRepo ? 'pointer' : 'not-allowed', opacity: selectedRepo ? 1 : 0.5 }}
            >
              ▶  Start Audit
            </button>
          </div>
        )}

        {/* Running: progress */}
        {phase === 'running' && !report && (
          <div className="fade-in">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20 }}>
              <span className="pulse" style={{ color: 'var(--accent)', fontSize: 13 }}>● Running scanners…</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
              {SCANNERS.map(sc => {
                const status = scanStatus[sc.id] || 'pending'
                const count  = scanCounts[sc.id] ?? null
                return (
                  <div key={sc.id} style={{ padding: '14px 16px', background: 'var(--bg2)', border: `1px solid ${status === 'running' ? 'var(--accent)' : status === 'done' ? 'var(--green)' : status === 'error' ? 'var(--red)' : 'var(--border)'}`, borderRadius: 'var(--radius)', transition: 'border-color 0.2s' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                      <span style={{ fontSize: 18 }}>{sc.icon}</span>
                      <span style={{ fontSize: 13, fontWeight: 500 }}>{sc.name}</span>
                      {sc.own && (
                        <span title="Built into RepoSage — not a wrapper around another tool"
                          style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: '0.05em', color: 'var(--accent)', border: '1px solid var(--accent)', borderRadius: 3, padding: '1px 4px', flexShrink: 0 }}>
                          OWN
                        </span>
                      )}
                      {status === 'running' && <span className="spin" style={{ marginLeft: 'auto', color: 'var(--accent)', fontSize: 12 }}>◌</span>}
                      {status === 'done'    && <span style={{ marginLeft: 'auto', color: 'var(--green)', fontSize: 12 }}>✓</span>}
                      {status === 'error'   && <span style={{ marginLeft: 'auto', color: 'var(--red)', fontSize: 12 }}>✕</span>}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--txt3)' }}>{sc.tool}</div>
                    {/* What this scanner is actually for. Spelled out because
                        the two RepoSage-own analyses are not documented
                        anywhere a reader would already know to look. */}
                    {sc.detects && (
                      <div style={{ fontSize: 10.5, color: 'var(--txt3)', lineHeight: 1.45, marginTop: 5 }}>
                        {sc.detects}
                      </div>
                    )}
                    {count !== null && (
                      <div style={{ marginTop: 6, fontSize: 12, color: count > 0 ? 'var(--yellow)' : 'var(--green)', fontWeight: 600 }}>
                        {count} finding{count !== 1 ? 's' : ''}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Done: full report */}
        {report && (
          <div className="fade-in">

            {/* Grade hero ── ── ── ── ── ── ── ── ── ── ── ── */}
            <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'stretch' }}>
              {/* Grade badge */}
              <div style={{ width: 110, flexShrink: 0, background: GRADE_STYLE[report.grade]?.bg, border: `2px solid ${GRADE_STYLE[report.grade]?.border}`, borderRadius: 'var(--radius-lg)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '16px 0' }}>
                <div style={{ fontSize: 48, fontWeight: 700, color: GRADE_STYLE[report.grade]?.color, lineHeight: 1 }}>{report.grade}</div>
                <div style={{ fontSize: 12, color: GRADE_STYLE[report.grade]?.color, marginTop: 4 }}>{report.score}/100</div>
                {report.source_loc > 0 && (
                  <div style={{ fontSize: 9.5, color: 'var(--txt3)', marginTop: 5, textAlign: 'center', lineHeight: 1.4 }}>
                    per {report.source_loc.toLocaleString()}<br/>source lines
                  </div>
                )}
              </div>

              {/* Severity counts — these are SOURCE findings, the ones graded */}
              <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8 }}>
                {['critical', 'high', 'medium', 'low', 'info'].map(sev => (
                  <button
                    key={sev}
                    onClick={() => setSevFilter(p => p === sev ? 'all' : sev)}
                    style={{ padding: '10px 6px', background: sevFilter === sev ? SEV[sev].bg : 'var(--bg2)', border: `1px solid ${sevFilter === sev ? SEV[sev].color : 'var(--border)'}`, borderRadius: 'var(--radius)', cursor: 'pointer', textAlign: 'center', transition: 'all 0.15s' }}
                  >
                    <div style={{ fontSize: 22, fontWeight: 700, color: SEV[sev].color }}>{sevCounts[sev] ?? 0}</div>
                    <div style={{ fontSize: 10, color: SEV[sev].color, marginTop: 2, textTransform: 'capitalize' }}>{sev}</div>
                  </button>
                ))}
              </div>

              {/* Meta */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, justifyContent: 'center', minWidth: 80, textAlign: 'center' }}>
                <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--txt)' }}>{totalFindings}</div>
                <div style={{ fontSize: 10, color: 'var(--txt3)' }}>findings</div>
                <div style={{ fontSize: 10, color: 'var(--txt3)' }}>{report.elapsed_s}s</div>
              </div>
            </div>

            {/* What the grade was actually computed from. A repo's tests, docs
                and examples are reported in full but not graded — a test file
                full of asserts is not a defect in the library, and counting it
                as one graded every mature project an F. */}
            {report.by_area && Object.keys(report.by_area).length > 1 && (
              <div style={{
                display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10,
                fontSize: 11.5, color: 'var(--txt3)', marginBottom: 14,
                background: 'var(--bg2)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius)', padding: '8px 12px',
              }}>
                <span style={{ color: 'var(--txt2)', fontWeight: 600 }}>
                  Graded on {report.graded_on ?? 0} finding{(report.graded_on ?? 0) === 1 ? '' : 's'} in source
                </span>
                {Object.entries(report.by_area)
                  .filter(([a]) => a !== 'source')
                  .map(([area, n]) => (
                    <span key={area}>
                      {/* Pluralise the noun, not the area label — `docs` is
                          already plural and was rendering as "13 in docss". */}
                      · {n} in {AREA_LABEL[area] || area} <span style={{ opacity: 0.6 }}>(not graded)</span>
                    </span>
                  ))}
              </div>
            )}

            {/* Scanner summary bar ── ── ── ── ── ── ── ── ── */}
            <div style={{ display: 'flex', gap: 6, marginBottom: 14, flexWrap: 'wrap' }}>
              <button
                onClick={() => setScanFilter('all')}
                style={{ fontSize: 11, padding: '4px 10px', borderRadius: 20, border: `1px solid ${scanFilter === 'all' ? 'var(--accent)' : 'var(--border)'}`, background: scanFilter === 'all' ? 'var(--accent-dim)' : 'transparent', color: scanFilter === 'all' ? 'var(--accent)' : 'var(--txt2)', cursor: 'pointer' }}
              >
                All scanners
              </button>
              {SCANNERS.map(sc => {
                const cnt = report.by_scanner?.[sc.id] ?? 0
                const active = scanFilter === sc.id
                return (
                  <button
                    key={sc.id}
                    onClick={() => setScanFilter(p => p === sc.id ? 'all' : sc.id)}
                    style={{ fontSize: 11, padding: '4px 10px', borderRadius: 20, border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`, background: active ? 'var(--accent-dim)' : 'transparent', color: active ? 'var(--accent)' : 'var(--txt2)', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5 }}
                  >
                    <span>{sc.icon}</span>
                    <span>{sc.name}</span>
                    {cnt > 0 && <span style={{ fontWeight: 600, color: 'var(--yellow)' }}>{cnt}</span>}
                  </button>
                )
              })}
            </div>

            {/* Tabs ── ── ── ── ── ── ── ── ── ── ── ── ── ── */}
            <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid var(--border)', marginBottom: 12 }}>
              {['findings', 'overview'].map(tab => (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  style={{ padding: '7px 14px', border: 'none', borderBottom: activeTab === tab ? '2px solid var(--accent)' : '2px solid transparent', background: 'transparent', color: activeTab === tab ? 'var(--txt)' : 'var(--txt3)', fontSize: 12, fontWeight: activeTab === tab ? 500 : 400, cursor: 'pointer', textTransform: 'capitalize' }}
                >
                  {tab}
                </button>
              ))}
              <div style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--txt3)', alignSelf: 'center', paddingRight: 4 }}>
                {filtered.length} / {totalFindings} shown
              </div>
            </div>

            {/* Findings table ── ── ── ── ── ── ── ── ── ── ── */}
            {activeTab === 'findings' && (
              <div>
                {filtered.length === 0 ? (
                  <div style={{ padding: '40px 0', textAlign: 'center', color: 'var(--txt3)', fontSize: 13 }}>
                    {totalFindings === 0 ? '🎉 No findings — clean repo!' : 'No findings match the current filter.'}
                  </div>
                ) : (
                  filtered.map((f, i) => {
                    const s = SEV[f.severity] || SEV.info
                    const isOpen = expandedFinding === i
                    return (
                      <div
                        key={i}
                        onClick={() => setExpandedFinding(isOpen ? null : i)}
                        style={{ marginBottom: 5, padding: '9px 12px', background: 'var(--bg2)', border: `1px solid ${isOpen ? s.color : 'var(--border)'}`, borderRadius: 'var(--radius)', cursor: 'pointer', transition: 'border-color 0.15s' }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          {/* Severity pill */}
                          <span style={{ fontSize: 10, padding: '2px 7px', borderRadius: 20, background: s.bg, color: s.color, fontWeight: 600, flexShrink: 0, minWidth: 56, textAlign: 'center', textTransform: 'capitalize' }}>
                            {f.severity}
                          </span>
                          {/* Scanner badge */}
                          <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0, width: 80, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {SCANNERS.find(sc => sc.id === f.scanner)?.icon} {f.scanner}
                          </span>
                          {/* Rule */}
                          {f.rule && (
                            <code style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--accent)', flexShrink: 0 }}>{f.rule}</code>
                          )}
                          {/* Message preview */}
                          <span style={{ fontSize: 12, color: 'var(--txt)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {f.message}
                          </span>
                          {/* Location */}
                          {f.file && (
                            <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {f.file}{f.line ? `:${f.line}` : ''}
                            </span>
                          )}
                          <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0 }}>{isOpen ? '▲' : '▼'}</span>
                        </div>

                        {/* Expanded detail */}
                        {isOpen && (
                          <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
                            <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr', gap: '6px 12px', fontSize: 12 }}>
                              {f.file && <><span style={{ color: 'var(--txt3)' }}>File</span><code style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--txt2)' }}>{f.file}{f.line ? `:${f.line}` : ''}</code></>}
                              {f.rule && <><span style={{ color: 'var(--txt3)' }}>Rule</span><code style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)' }}>{f.rule}</code></>}
                              {f.confidence && <><span style={{ color: 'var(--txt3)' }}>Confidence</span><span style={{ color: 'var(--txt2)' }}>{f.confidence}</span></>}
                              <span style={{ color: 'var(--txt3)' }}>Message</span>
                              <span style={{ color: 'var(--txt)', lineHeight: 1.5 }}>{f.message}</span>
                            </div>

                            {/* The offending code. Captured by the backend while
                                the clone still existed — see tester.py. */}
                            {f.code?.length > 0 && (
                              <div style={{ marginTop: 10 }}>
                                <div style={{ fontSize: 10, color: 'var(--txt3)', marginBottom: 5, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                                  Code — {f.file}:{f.line}
                                </div>
                                <CodeSnippet code={f.code} start={f.code_start} line={f.line} color={s.color} />
                              </div>
                            )}

                            {/* RepoSage's own rules are not documented anywhere
                                else, so the explanation has to travel with the
                                finding. */}
                            {RULE_INFO[f.rule] && (
                              <div style={{ marginTop: 10, padding: '9px 11px', background: 'rgba(93,142,255,0.06)', border: '1px solid rgba(93,142,255,0.25)', borderRadius: 6 }}>
                                <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--accent)', marginBottom: 4 }}>
                                  {RULE_INFO[f.rule][0]}
                                  <span style={{ fontSize: 9, fontWeight: 500, color: 'var(--txt3)', marginLeft: 8, letterSpacing: '0.06em' }}>
                                    REPOSAGE ANALYSIS
                                  </span>
                                </div>
                                <div style={{ fontSize: 11.5, color: 'var(--txt2)', lineHeight: 1.55, marginBottom: 6 }}>
                                  {RULE_INFO[f.rule][1]}
                                </div>
                                <div style={{ fontSize: 11.5, color: 'var(--txt2)', lineHeight: 1.55 }}>
                                  <strong style={{ color: 'var(--txt)' }}>What to do: </strong>
                                  {RULE_INFO[f.rule][2]}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })
                )}
              </div>
            )}

            {/* Overview tab ── ── ── ── ── ── ── ── ── ── ── */}
            {activeTab === 'overview' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {/* Score breakdown */}
                <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '14px 18px' }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Score breakdown</div>
                  {['critical','high','medium','low','info'].map(sev => {
                    const cnt = sevCounts[sev] || 0
                    const exposure = CVSS_WEIGHTS[sev] * cnt
                    return (
                      <div key={sev} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                        <span style={{ fontSize: 11, color: SEV[sev].color, width: 56, textTransform: 'capitalize' }}>{sev}</span>
                        <div style={{ flex: 1, height: 6, background: 'var(--bg3)', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${Math.min(100, cnt * 5)}%`, background: SEV[sev].color, borderRadius: 3 }} />
                        </div>
                        <span style={{ fontSize: 11, color: 'var(--txt2)', width: 28, textAlign: 'right' }}>{cnt}</span>
                        {exposure > 0 && <span style={{ fontSize: 10, color: 'var(--txt3)', width: 76, textAlign: 'right' }}>{CVSS_WEIGHTS[sev].toFixed(1)} × {cnt} CVSS</span>}
                      </div>
                    )
                  })}
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)', fontSize: 12, display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: 'var(--txt2)' }}>Final score</span>
                    <span style={{ fontWeight: 700, color: GRADE_STYLE[report.grade]?.color }}>{report.score}/100 (Grade {report.grade})</span>
                  </div>
                  <div style={{ marginTop: 6, fontSize: 9.5, color: 'var(--txt3)', lineHeight: 1.5 }}>
                    Score = 100 × e <sup style={{ fontSize: 8 }}>−exposure/40</sup> — exposure is the CVSS-weighted sum of all findings (CVSS v3.1 severity bands). Diminishing marginal penalty per finding, no hard floor at zero.
                  </div>
                </div>

                {/* Per-scanner counts */}
                <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '14px 18px' }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Findings per scanner</div>
                  {SCANNERS.map(sc => {
                    const cnt = report.by_scanner?.[sc.id] ?? 0
                    const max = Math.max(...SCANNERS.map(s => report.by_scanner?.[s.id] ?? 0), 1)
                    return (
                      <div key={sc.id} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                        <span style={{ fontSize: 14, width: 20 }}>{sc.icon}</span>
                        <span style={{ fontSize: 11, color: 'var(--txt2)', width: 110, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sc.name}</span>
                        <div style={{ flex: 1, height: 6, background: 'var(--bg3)', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${(cnt / max) * 100}%`, background: cnt > 0 ? 'var(--accent)' : 'var(--bg3)', borderRadius: 3 }} />
                        </div>
                        <span style={{ fontSize: 11, color: cnt > 0 ? 'var(--yellow)' : 'var(--green)', width: 24, textAlign: 'right' }}>{cnt}</span>
                      </div>
                    )
                  })}
                </div>

                {/* Pipeline summary */}
                <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '14px 18px', gridColumn: '1/-1' }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Audit pipeline</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 0, flexWrap: 'wrap' }}>
                    {[
                      { label: 'GitHub Repo', icon: '🔗' },
                      { label: 'Clone / Load', icon: '📥' },
                      ...SCANNERS.map(sc => ({ label: sc.name, icon: sc.icon, id: sc.id })),
                      { label: 'Normalize', icon: '⚙️' },
                      { label: 'Score', icon: '📊' },
                      { label: 'Audit Report', icon: '📋' },
                    ].map((node, i, arr) => (
                      <div key={i} style={{ display: 'flex', alignItems: 'center' }}>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                          <div style={{ width: 36, height: 36, borderRadius: 8, background: node.id ? (scanStatus[node.id] === 'done' ? 'var(--green-dim)' : 'var(--bg3)') : 'var(--bg3)', border: `1px solid ${node.id && scanStatus[node.id] === 'done' ? 'var(--green)' : 'var(--border)'}`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16 }}>
                            {node.icon}
                          </div>
                          <span style={{ fontSize: 9, color: 'var(--txt3)', textAlign: 'center', maxWidth: 48, lineHeight: 1.3 }}>{node.label}</span>
                        </div>
                        {i < arr.length - 1 && (
                          <div style={{ width: 16, height: 1, background: 'var(--border2)', margin: '0 2px', marginBottom: 14 }} />
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

          </div>
        )}
      </div>
    </div>
  )
}
