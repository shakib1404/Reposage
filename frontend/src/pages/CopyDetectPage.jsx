import { useState, useRef, useEffect } from 'react'
import { streamCopyDetect, getCorpusStatus, streamCorpusAdd, streamCorpusSearch } from '../api'

// ── Similarity colour helpers ─────────────────────────────────────────────────

function simColor(sim) {
  if (sim >= 0.8) return '#f87171'  // high  → red
  if (sim >= 0.5) return '#fbbf24'  // mid   → yellow
  return '#34d399'                   // low   → green
}

function SimBar({ value }) {
  const pct = Math.round(value * 100)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div style={{ flex: 1, height: 5, background: 'var(--bg3)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${pct}%`, background: simColor(value), borderRadius: 3, transition: 'width 0.3s' }} />
      </div>
      <span style={{ fontSize: 11, fontWeight: 600, color: simColor(value), minWidth: 36 }}>{pct}%</span>
    </div>
  )
}

// ── 3-dots separator (the "..." extra feature between matched slices) ──────────

function ThreeDotsSeparator() {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 0,
      margin: '4px 0',
    }}>
      {/* left code panel dots */}
      <div style={{
        flex: 1,
        textAlign: 'center',
        fontSize: 13,
        letterSpacing: 4,
        color: 'var(--txt3)',
        fontFamily: 'var(--mono)',
        background: 'var(--bg3)',
        borderRadius: 4,
        padding: '3px 0',
        borderLeft: '3px solid var(--accent)',
      }}>...</div>

      {/* middle arrow column */}
      <div style={{ width: 36, textAlign: 'center', fontSize: 11, color: 'var(--txt3)', flexShrink: 0 }}>↕</div>

      {/* right code panel dots */}
      <div style={{
        flex: 1,
        textAlign: 'center',
        fontSize: 13,
        letterSpacing: 4,
        color: 'var(--txt3)',
        fontFamily: 'var(--mono)',
        background: 'var(--bg3)',
        borderRadius: 4,
        padding: '3px 0',
        borderLeft: '3px solid var(--green)',
      }}>...</div>
    </div>
  )
}

// ── Code panel ────────────────────────────────────────────────────────────────

function CodePanel({ label, lines, code, accent }) {
  if (!code) return (
    <div style={{ flex: 1, padding: '10px 12px', background: 'var(--bg3)', borderRadius: 6, fontSize: 11, color: 'var(--txt3)', fontFamily: 'var(--mono)', borderLeft: `3px solid ${accent}` }}>
      (no code)
    </div>
  )

  return (
    <div style={{ flex: 1, overflow: 'hidden', borderRadius: 6, borderLeft: `3px solid ${accent}` }}>
      {/* line range badge */}
      <div style={{ padding: '4px 10px', background: 'var(--bg3)', fontSize: 10, color: 'var(--txt3)', fontFamily: 'var(--mono)' }}>
        <span style={{ color: accent, fontWeight: 600 }}>{label}</span>
        {lines && <span style={{ marginLeft: 6 }}>lines {lines[0]}–{lines[1]}</span>}
      </div>
      <pre style={{
        margin: 0,
        padding: '8px 10px',
        fontSize: 11,
        fontFamily: 'var(--mono)',
        lineHeight: 1.6,
        background: 'var(--bg2)',
        overflowX: 'auto',
        whiteSpace: 'pre',
        maxHeight: 260,
        overflowY: 'auto',
        color: 'var(--txt)',
      }}>
        {code}
      </pre>
    </div>
  )
}

// ── Single detection card ─────────────────────────────────────────────────────

function DetectionCard({ det, index }) {
  const [expanded, setExpanded] = useState(index === 0)

  return (
    <div style={{
      marginBottom: 10,
      border: `1px solid ${expanded ? simColor(det.similarity) : 'var(--border)'}`,
      borderRadius: 'var(--radius)',
      background: 'var(--bg2)',
      overflow: 'hidden',
      transition: 'border-color 0.15s',
    }}>
      {/* ── Header row ── */}
      <div
        onClick={() => setExpanded(p => !p)}
        style={{ padding: '10px 14px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10 }}
      >
        {/* Similarity pill */}
        <div style={{
          fontSize: 12, fontWeight: 700,
          color: simColor(det.similarity),
          background: `${simColor(det.similarity)}22`,
          border: `1px solid ${simColor(det.similarity)}`,
          borderRadius: 20, padding: '2px 10px', flexShrink: 0,
        }}>
          {Math.round(det.similarity * 100)}%
        </div>

        {/* Files */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span style={{ color: 'var(--accent)', fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{det.test_file}</span>
            <span style={{ color: 'var(--txt3)', flexShrink: 0 }}>→</span>
            <span style={{ color: 'var(--green)', fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{det.source_file}</span>
          </div>
          <SimBar value={det.similarity} />
        </div>

        {/* Token overlap */}
        <div style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0, textAlign: 'right' }}>
          <div>{det.token_overlap} tokens</div>
          <div>{(det.slices || []).length} match{(det.slices || []).length !== 1 ? 'es' : ''}</div>
        </div>

        {/* Chevron */}
        <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0 }}>{expanded ? '▲' : '▼'}</span>
      </div>

      {/* ── Expanded: per-slice code view ── */}
      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '12px 14px' }}>
          {/* File similarity breakdown */}
          <div style={{ display: 'flex', gap: 16, marginBottom: 12, fontSize: 11, color: 'var(--txt3)' }}>
            <span>
              Test coverage:&nbsp;
              <strong style={{ color: 'var(--accent)' }}>{Math.round(det.similarity_test * 100)}%</strong>
            </span>
            <span>
              Source coverage:&nbsp;
              <strong style={{ color: 'var(--green)' }}>{Math.round(det.similarity_source * 100)}%</strong>
            </span>
          </div>

          {/* Slices — with "..." separators between them */}
          {(det.slices || []).length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--txt3)', fontStyle: 'italic' }}>
              No code slices available (overlap detected via fingerprint).
            </div>
          ) : (
            det.slices.map((slice, si) => (
              <div key={si}>
                {/* 3-dots separator between slices — the "..." extra feature */}
                {si > 0 && <ThreeDotsSeparator />}

                {/* Side-by-side code panels */}
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                  <CodePanel
                    label="Test"
                    lines={slice.test_lines}
                    code={slice.test_code}
                    accent="var(--accent)"
                  />

                  {/* Center column: match arrow */}
                  <div style={{
                    width: 28, flexShrink: 0, display: 'flex', alignItems: 'center',
                    justifyContent: 'center', paddingTop: 30,
                  }}>
                    <span style={{ fontSize: 14, color: simColor(det.similarity) }}>←</span>
                  </div>

                  <CodePanel
                    label="Source"
                    lines={slice.source_lines}
                    code={slice.source_code}
                    accent="var(--green)"
                  />
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

// ── Single duplicate-function card (self-scan mode) ─────────────────────────────

function DuplicateCard({ dup, index }) {
  const [expanded, setExpanded] = useState(index === 0)
  const sameScope = dup.file_a === dup.file_b && dup.class_a === dup.class_b
  const scopeLabel = sameScope
    ? (dup.class_a ? `class ${dup.class_a}` : 'module-level')
    : `cross-class: ${dup.class_a || 'module'} → ${dup.class_b || 'module'}`

  return (
    <div style={{
      marginBottom: 10,
      border: `1px solid ${expanded ? simColor(dup.similarity) : 'var(--border)'}`,
      borderRadius: 'var(--radius)',
      background: 'var(--bg2)',
      overflow: 'hidden',
      transition: 'border-color 0.15s',
    }}>
      <div
        onClick={() => setExpanded(p => !p)}
        style={{ padding: '10px 14px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10 }}
      >
        <div style={{
          fontSize: 12, fontWeight: 700,
          color: simColor(dup.similarity),
          background: `${simColor(dup.similarity)}22`,
          border: `1px solid ${simColor(dup.similarity)}`,
          borderRadius: 20, padding: '2px 10px', flexShrink: 0,
        }}>
          {Math.round(dup.similarity * 100)}%
        </div>

        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span style={{ color: 'var(--accent)', fontFamily: 'var(--mono)' }}>{dup.function_a}</span>
            <span style={{ color: 'var(--txt3)', flexShrink: 0 }}>≈</span>
            <span style={{ color: 'var(--green)', fontFamily: 'var(--mono)' }}>{dup.function_b}</span>
          </div>
          <div style={{ fontSize: 10, color: 'var(--txt3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {sameScope ? `${dup.file_a} · ${scopeLabel}` : `${dup.file_a} → ${dup.file_b} · ${scopeLabel}`}
          </div>
        </div>

        <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0 }}>{expanded ? '▲' : '▼'}</span>
      </div>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '12px 14px' }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <CodePanel label={dup.function_a} lines={dup.lines_a} code={dup.code_a} accent="var(--accent)" />
            <div style={{ width: 28, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', paddingTop: 30 }}>
              <span style={{ fontSize: 14, color: simColor(dup.similarity) }}>≈</span>
            </div>
            <CodePanel label={dup.function_b} lines={dup.lines_b} code={dup.code_b} accent="var(--green)" />
          </div>
        </div>
      )}
    </div>
  )
}

// ── Single corpus-match card ─────────────────────────────────────────────────────

// A bare method name is ambiguous across classes — `_strip_punc_if_word` says
// nothing about which class it belongs to, and the backend already sends the
// scope. `null` means module level, which is itself worth showing.
function qualify(cls, fn) {
  return cls ? `${cls}.${fn}` : fn
}

function CorpusMatchCard({ m, index }) {
  const [expanded, setExpanded] = useState(index === 0)
  const moduleLevel = !m.query_class && !m.matched_class

  return (
    <div style={{
      marginBottom: 10,
      border: `1px solid ${expanded ? simColor(m.similarity) : 'var(--border)'}`,
      borderRadius: 'var(--radius)',
      background: 'var(--bg2)',
      overflow: 'hidden',
      transition: 'border-color 0.15s',
    }}>
      <div
        onClick={() => setExpanded(p => !p)}
        style={{ padding: '10px 14px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10 }}
      >
        <div style={{
          fontSize: 12, fontWeight: 700,
          color: simColor(m.similarity),
          background: `${simColor(m.similarity)}22`,
          border: `1px solid ${simColor(m.similarity)}`,
          borderRadius: 20, padding: '2px 10px', flexShrink: 0,
        }}>
          {Math.round(m.similarity * 100)}%
        </div>

        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span style={{ color: 'var(--accent)', fontFamily: 'var(--mono)' }}>{qualify(m.query_class, m.query_function)}</span>
            <span style={{ color: 'var(--txt3)', flexShrink: 0 }}>≈</span>
            <span style={{ color: 'var(--green)', fontFamily: 'var(--mono)' }}>{qualify(m.matched_class, m.matched_function)}</span>
            <span style={{
              fontSize: 9, letterSpacing: '0.04em', textTransform: 'uppercase',
              color: 'var(--txt3)', border: '1px solid var(--border2)',
              borderRadius: 3, padding: '1px 5px', flexShrink: 0,
            }}>
              {moduleLevel ? 'module' : 'class'}
            </span>
          </div>
          <div style={{ fontSize: 10, color: 'var(--txt3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {m.query_file} → <strong style={{ color: 'var(--txt2)' }}>{m.matched_repo}</strong>:{m.matched_file}
          </div>
        </div>

        <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0 }}>{expanded ? '▲' : '▼'}</span>
      </div>

      {expanded && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '12px 14px' }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <CodePanel label={`this repo — ${qualify(m.query_class, m.query_function)}`} lines={m.query_lines} code={m.query_code} accent="var(--accent)" />
            <div style={{ width: 28, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', paddingTop: 30 }}>
              <span style={{ fontSize: 14, color: simColor(m.similarity) }}>≈</span>
            </div>
            <CodePanel label={`${m.matched_repo} — ${qualify(m.matched_class, m.matched_function)}`} lines={m.matched_lines} code={m.matched_code} accent="var(--green)" />
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function CopyDetectPage() {
  const [mode,            setMode]           = useState('cross_repo')   // 'cross_repo' | 'self_scan' | 'corpus'
  const [testRepo,        setTestRepo]       = useState('')
  const [sourceRepo,      setSourceRepo]     = useState('')
  const [minSim,          setMinSim]         = useState(0.5)
  const [dupThreshold,    setDupThreshold]   = useState(0.68)
  const [dupScope,        setDupScope]       = useState('scoped')   // 'scoped' | 'repo_wide'
  const [fileTypesStr,    setFileTypesStr]   = useState('')
  const [phase,           setPhase]          = useState('idle')   // idle | running | done | error
  const [log,             setLog]            = useState([])
  const [detections,      setDetections]     = useState([])   // cross_repo results
  const [duplicates,      setDuplicates]     = useState([])   // self_scan results
  const [progress,        setProgress]       = useState({ current: 0, total: 0, file: '' })
  const [info,            setInfo]           = useState(null)
  const cleanupRef = useRef(null)
  const logEndRef  = useRef(null)

  // ── Corpus mode ──────────────────────────────────────────────────────────
  const [corpusInfo,      setCorpusInfo]     = useState(null)   // { repo_count, function_count, repos }
  const [corpusMatches,   setCorpusMatches]  = useState([])
  const [corpusSummary,   setCorpusSummary]  = useState([])
  const [corpusThreshold, setCorpusThreshold] = useState(0.75)
  const [corpusTopK,      setCorpusTopK]     = useState(5)
  const [corpusAction,    setCorpusAction]   = useState(null)   // 'add' | 'search' | null

  function refreshCorpusStatus() {
    getCorpusStatus().then(setCorpusInfo).catch(() => {})
  }

  useEffect(() => {
    if (mode === 'corpus' && !corpusInfo) refreshCorpusStatus()
  }, [mode])

  function startCorpusAdd() {
    if (!testRepo.trim()) return
    setPhase('running')
    setCorpusAction('add')
    setLog([])
    setCorpusMatches([])
    setCorpusSummary([])

    const cleanup = streamCorpusAdd(testRepo.trim(), (ev) => {
      if (ev.type === 'status') { addLog(ev.message, 'status'); return }
      if (ev.type === 'done') {
        setPhase('done')
        addLog(ev.message || `Added ${ev.functions_added} function(s) from ${ev.repo}.`, 'done')
        refreshCorpusStatus()
        return
      }
      if (ev.type === 'error') { setPhase('error'); addLog(`Error: ${ev.message}`, 'error') }
    })
    cleanupRef.current = cleanup
  }

  function startCorpusSearch() {
    if (!testRepo.trim()) return
    setPhase('running')
    setCorpusAction('search')
    setLog([])
    setCorpusMatches([])
    setCorpusSummary([])
    setInfo(null)

    const cleanup = streamCorpusSearch(testRepo.trim(), corpusThreshold, corpusTopK, (ev) => {
      if (ev.type === 'status') { addLog(ev.message, 'status'); return }
      if (ev.type === 'info') {
        setInfo(ev)
        addLog(ev.message, 'info')
        return
      }
      if (ev.type === 'match') {
        setCorpusMatches(prev => [...prev, ev])
        addLog(`Match: ${ev.query_function} ↔ ${ev.matched_repo}:${ev.matched_function} (${Math.round(ev.similarity * 100)}%)`, 'match')
        return
      }
      if (ev.type === 'summary') { setCorpusSummary(ev.repos); return }
      if (ev.type === 'done') {
        setPhase('done')
        addLog(`Done — ${ev.total_matches} match(es) across ${ev.repo_matches.length} repo(s).`, 'done')
        return
      }
      if (ev.type === 'error') { setPhase('error'); addLog(`Error: ${ev.message}`, 'error') }
    })
    cleanupRef.current = cleanup
  }

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  function addLog(text, type = 'status') {
    setLog(prev => [...prev, { text, type }])
  }

  function startDetection() {
    if (!testRepo.trim()) return
    if (mode === 'cross_repo' && !sourceRepo.trim()) return
    setPhase('running')
    setLog([])
    setDetections([])
    setDuplicates([])
    setProgress({ current: 0, total: 0, file: '' })
    setInfo(null)

    const fileTypes = fileTypesStr.trim()
      ? fileTypesStr.split(',').map(s => s.trim().replace(/^\./, '')).filter(Boolean)
      : []

    const cleanup = streamCopyDetect(
      {
        mode,
        testRepo:      testRepo.trim(),
        sourceRepo:    sourceRepo.trim(),
        minSimilarity: minSim,
        dupThreshold,
        dupScope,
        fileTypes,
      },
      (ev) => {
        if (ev.type === 'status') {
          addLog(ev.message, 'status')
          return
        }
        if (ev.type === 'info') {
          setInfo(ev)
          addLog(ev.message, 'info')
          return
        }
        if (ev.type === 'progress') {
          setProgress({ current: ev.current, total: ev.total, file: ev.file })
          return
        }
        if (ev.type === 'detection') {
          setDetections(prev => [...prev, ev])
          addLog(`Match: ${ev.test_file} ↔ ${ev.source_file} (${Math.round(ev.similarity * 100)}%)`, 'match')
          return
        }
        if (ev.type === 'duplicate') {
          setDuplicates(prev => [...prev, ev])
          const scopeLabel = ev.class_a === ev.class_b && ev.file_a === ev.file_b
            ? (ev.class_a ? `class ${ev.class_a}` : 'module level')
            : 'cross-class'
          addLog(`Duplicate (${scopeLabel}): ${ev.function_a} ↔ ${ev.function_b} (${Math.round(ev.similarity * 100)}%)`, 'match')
          return
        }
        if (ev.type === 'done') {
          setPhase('done')
          const n = mode === 'self_scan' ? ev.total_duplicates : ev.total_detections
          addLog(`Done — ${n} match(es) found.`, 'done')
          return
        }
        if (ev.type === 'error') {
          setPhase('error')
          addLog(`Error: ${ev.message}`, 'error')
        }
      },
    )

    cleanupRef.current = cleanup
  }

  function stopDetection() {
    cleanupRef.current?.()
    setPhase('idle')
    addLog('Stopped by user.', 'warning')
  }

  const isRunning  = phase === 'running'
  const isDone     = phase === 'done'
  const progressPct = progress.total > 0 ? Math.round((progress.current / progress.total) * 100) : 0
  const results = mode === 'cross_repo' ? detections : mode === 'self_scan' ? duplicates : corpusMatches

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 97px)', overflow: 'hidden' }}>

      {/* ── Left panel: inputs + controls + log ──────────────────────────── */}
      <div style={{ width: 310, flexShrink: 0, borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>

        <div style={{ padding: '16px 16px 0' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Copy Detector</div>
          <div style={{ fontSize: 11, color: 'var(--txt2)', marginBottom: 12 }}>
            {mode === 'cross_repo' && 'Detect copied / vendored code between two repos'}
            {mode === 'self_scan'  && 'Find semantically duplicated functions within one repo'}
            {mode === 'corpus'     && 'Check one repo against a growing corpus of indexed repos'}
          </div>

          {/* Mode toggle */}
          <div style={{ display: 'flex', gap: 4, marginBottom: 14, background: 'var(--bg2)', borderRadius: 'var(--radius)', padding: 3 }}>
            {[
              { id: 'cross_repo', label: '2 Repos' },
              { id: 'self_scan',  label: '1 Repo' },
              { id: 'corpus',     label: 'Corpus' },
            ].map(m => (
              <button
                key={m.id}
                onClick={() => !isRunning && setMode(m.id)}
                disabled={isRunning}
                style={{
                  flex: 1, padding: '6px 4px', fontSize: 11, fontWeight: 500,
                  border: 'none', borderRadius: 6, cursor: isRunning ? 'default' : 'pointer',
                  background: mode === m.id ? 'var(--accent)' : 'transparent',
                  color:      mode === m.id ? 'white' : 'var(--txt3)',
                  transition: 'background 0.15s',
                }}
              >
                {m.label}
              </button>
            ))}
          </div>

          {/* Test repo input */}
          <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>
            {mode === 'cross_repo' ? 'Test Repo URL or path' : 'Repo URL or path'}
          </label>
          <input
            value={testRepo}
            onChange={e => setTestRepo(e.target.value)}
            placeholder="https://github.com/owner/repo"
            disabled={isRunning}
            style={{
              width: '100%', boxSizing: 'border-box',
              padding: '7px 10px', marginBottom: 10,
              background: 'var(--bg2)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius)', color: 'var(--txt)', fontSize: 12,
              fontFamily: 'var(--mono)',
            }}
          />

          {mode === 'cross_repo' && (
            <>
              {/* Source repo input */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>Source Repo URL or path</label>
              <input
                value={sourceRepo}
                onChange={e => setSourceRepo(e.target.value)}
                placeholder="https://github.com/owner/repo"
                disabled={isRunning}
                style={{
                  width: '100%', boxSizing: 'border-box',
                  padding: '7px 10px', marginBottom: 10,
                  background: 'var(--bg2)', border: '1px solid var(--border)',
                  borderRadius: 'var(--radius)', color: 'var(--txt)', fontSize: 12,
                  fontFamily: 'var(--mono)',
                }}
              />

              {/* Min similarity */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>
                Min similarity:&nbsp;
                <strong style={{ color: simColor(minSim) }}>{Math.round(minSim * 100)}%</strong>
              </label>
              <input
                type="range" min="0.1" max="1.0" step="0.05"
                value={minSim}
                onChange={e => setMinSim(parseFloat(e.target.value))}
                disabled={isRunning}
                style={{ width: '100%', marginBottom: 10, accentColor: simColor(minSim) }}
              />

              {/* File types filter */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>
                File types (optional, e.g. <code style={{ fontFamily: 'var(--mono)' }}>py,js,ts</code>)
              </label>
              <input
                value={fileTypesStr}
                onChange={e => setFileTypesStr(e.target.value)}
                placeholder="Leave blank for all code files"
                disabled={isRunning}
                style={{
                  width: '100%', boxSizing: 'border-box',
                  padding: '7px 10px', marginBottom: 14,
                  background: 'var(--bg2)', border: '1px solid var(--border)',
                  borderRadius: 'var(--radius)', color: 'var(--txt)', fontSize: 12,
                  fontFamily: 'var(--mono)',
                }}
              />
            </>
          )}

          {mode === 'self_scan' && (
            <>
              {/* Scope toggle */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>Compare scope</label>
              <div style={{ display: 'flex', gap: 4, marginBottom: 10, background: 'var(--bg2)', borderRadius: 'var(--radius)', padding: 3 }}>
                {[
                  { id: 'scoped',    label: 'Same class/module' },
                  { id: 'repo_wide', label: 'Whole repo' },
                ].map(s => (
                  <button
                    key={s.id}
                    onClick={() => !isRunning && setDupScope(s.id)}
                    disabled={isRunning}
                    style={{
                      flex: 1, padding: '6px 4px', fontSize: 11, fontWeight: 500,
                      border: 'none', borderRadius: 6, cursor: isRunning ? 'default' : 'pointer',
                      background: dupScope === s.id ? 'var(--accent)' : 'transparent',
                      color:      dupScope === s.id ? 'white' : 'var(--txt3)',
                      transition: 'background 0.15s',
                    }}
                  >
                    {s.label}
                  </button>
                ))}
              </div>

              {/* Duplicate threshold */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>
                Similarity threshold:&nbsp;
                <strong style={{ color: simColor(dupThreshold) }}>{Math.round(dupThreshold * 100)}%</strong>
              </label>
              <input
                type="range" min="0.5" max="0.95" step="0.01"
                value={dupThreshold}
                onChange={e => setDupThreshold(parseFloat(e.target.value))}
                disabled={isRunning}
                style={{ width: '100%', marginBottom: 6, accentColor: simColor(dupThreshold) }}
              />
              <div style={{ fontSize: 10, color: 'var(--txt3)', marginBottom: 14, lineHeight: 1.5 }}>
                {dupScope === 'scoped'
                  ? 'Compares functions within the same class (or module-level functions in the same file) — tightest, most directly actionable.'
                  : 'Compares every function against every other function in the repo — also catches duplicate logic copy-pasted across different classes/files, at the cost of more incidental matches. Consider raising the threshold.'}
                {' '}Only Python files are scanned.
              </div>
            </>
          )}

          {mode === 'corpus' && (
            <>
              {/* Corpus status */}
              <div style={{ fontSize: 11, color: 'var(--txt2)', background: 'var(--bg3)', borderRadius: 6, padding: '8px 10px', marginBottom: 12, lineHeight: 1.6 }}>
                {corpusInfo
                  ? <>Corpus: <strong>{corpusInfo.repo_count}</strong> repo{corpusInfo.repo_count !== 1 ? 's' : ''},{' '}
                      <strong>{corpusInfo.function_count}</strong> function{corpusInfo.function_count !== 1 ? 's' : ''} indexed</>
                  : 'Loading corpus status…'}
              </div>

              {/* Match threshold */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>
                Match threshold:&nbsp;
                <strong style={{ color: simColor(corpusThreshold) }}>{Math.round(corpusThreshold * 100)}%</strong>
              </label>
              <input
                type="range" min="0.5" max="0.95" step="0.01"
                value={corpusThreshold}
                onChange={e => setCorpusThreshold(parseFloat(e.target.value))}
                disabled={isRunning}
                style={{ width: '100%', marginBottom: 10, accentColor: simColor(corpusThreshold) }}
              />

              {/* Top-K */}
              <label style={{ fontSize: 11, color: 'var(--txt3)', display: 'block', marginBottom: 3 }}>
                Neighbors per function: <strong style={{ color: 'var(--txt2)' }}>{corpusTopK}</strong>
              </label>
              <input
                type="range" min="1" max="10" step="1"
                value={corpusTopK}
                onChange={e => setCorpusTopK(parseInt(e.target.value))}
                disabled={isRunning}
                style={{ width: '100%', marginBottom: 14, accentColor: 'var(--accent)' }}
              />
            </>
          )}

          {/* Action button(s) */}
          {isRunning ? (
            <button
              onClick={stopDetection}
              style={{
                width: '100%', padding: '9px 0',
                background: 'transparent', border: '1px solid var(--red)',
                borderRadius: 'var(--radius)', color: 'var(--red)',
                fontSize: 13, fontWeight: 500, cursor: 'pointer', marginBottom: 12,
              }}
            >
              ■ Stop
            </button>
          ) : mode === 'corpus' ? (
            <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
              <button
                onClick={startCorpusAdd}
                disabled={!testRepo.trim()}
                title="Index this repo's functions into the corpus"
                style={{
                  flex: 1, padding: '9px 0',
                  background: 'transparent', border: '1px solid var(--border2)',
                  borderRadius: 'var(--radius)', color: testRepo.trim() ? 'var(--txt)' : 'var(--txt3)',
                  fontSize: 12, fontWeight: 500,
                  cursor: testRepo.trim() ? 'pointer' : 'not-allowed',
                  opacity: testRepo.trim() ? 1 : 0.5,
                }}
              >
                ➕ Add to corpus
              </button>
              <button
                onClick={startCorpusSearch}
                disabled={!testRepo.trim()}
                title="Find matches for this repo within the corpus"
                style={{
                  flex: 1, padding: '9px 0',
                  background: 'var(--accent)', border: 'none',
                  borderRadius: 'var(--radius)', color: 'white',
                  fontSize: 12, fontWeight: 500,
                  cursor: testRepo.trim() ? 'pointer' : 'not-allowed',
                  opacity: testRepo.trim() ? 1 : 0.5,
                }}
              >
                🔍 Search corpus
              </button>
            </div>
          ) : (
            <button
              onClick={startDetection}
              disabled={!testRepo.trim() || (mode === 'cross_repo' && !sourceRepo.trim())}
              style={{
                width: '100%', padding: '9px 0',
                background: 'var(--accent)', border: 'none',
                borderRadius: 'var(--radius)', color: 'white',
                fontSize: 13, fontWeight: 500,
                cursor: (testRepo.trim() && (mode !== 'cross_repo' || sourceRepo.trim())) ? 'pointer' : 'not-allowed',
                opacity: (testRepo.trim() && (mode !== 'cross_repo' || sourceRepo.trim())) ? 1 : 0.5,
                marginBottom: 12,
              }}
            >
              {isDone ? '↺ Re-run' : '▶ Detect'}
            </button>
          )}

          {/* Progress bar */}
          {isRunning && progress.total > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ height: 4, background: 'var(--bg3)', borderRadius: 2, overflow: 'hidden', marginBottom: 4 }}>
                <div style={{ height: '100%', width: `${progressPct}%`, background: 'var(--accent)', borderRadius: 2, transition: 'width 0.2s' }} />
              </div>
              <div style={{ fontSize: 10, color: 'var(--txt3)', fontFamily: 'var(--mono)' }}>
                {mode === 'cross_repo'
                  ? `${progress.current}/${progress.total} — ${progress.file}`
                  : `${progress.current}/${progress.total} scopes compared`}
              </div>
            </div>
          )}

          {/* Info block */}
          {info && mode === 'cross_repo' && (
            <div style={{ fontSize: 11, color: 'var(--txt2)', background: 'var(--bg3)', borderRadius: 6, padding: '8px 10px', marginBottom: 10, lineHeight: 1.6 }}>
              <div>Test files: <strong>{info.test_files}</strong></div>
              <div>Source files: <strong>{info.source_files}</strong></div>
            </div>
          )}
          {info && mode === 'self_scan' && (
            <div style={{ fontSize: 11, color: 'var(--txt2)', background: 'var(--bg3)', borderRadius: 6, padding: '8px 10px', marginBottom: 10, lineHeight: 1.6 }}>
              <div>Functions scanned: <strong>{info.total_functions}</strong></div>
              <div>Files scanned: <strong>{info.total_files}</strong></div>
            </div>
          )}
          {info && mode === 'corpus' && (
            <div style={{ fontSize: 11, color: 'var(--txt2)', background: 'var(--bg3)', borderRadius: 6, padding: '8px 10px', marginBottom: 10, lineHeight: 1.6 }}>
              <div>This repo's functions: <strong>{info.total_functions}</strong></div>
              <div>Corpus searched: <strong>{info.corpus_repos}</strong> repos / <strong>{info.corpus_functions}</strong> functions</div>
            </div>
          )}
        </div>

        {/* Live log */}
        <div style={{ padding: '0 16px 16px', flex: 1, minHeight: 0 }}>
          <div style={{ fontSize: 10, fontWeight: 500, color: 'var(--txt3)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.07em' }}>Log</div>
          <div style={{ fontSize: 11, fontFamily: 'var(--mono)', lineHeight: 1.6 }}>
            {log.map((entry, i) => (
              <div key={i} style={{
                color: entry.type === 'error'   ? 'var(--red)'    :
                       entry.type === 'done'    ? 'var(--green)'  :
                       entry.type === 'match'   ? 'var(--accent)' :
                       entry.type === 'warning' ? 'var(--yellow)' :
                       entry.type === 'info'    ? 'var(--txt2)'   : 'var(--txt3)',
                marginBottom: 2,
                wordBreak: 'break-word',
              }}>
                {entry.text}
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>

      {/* ── Right panel: results ──────────────────────────────────────────── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>

        {/* Idle / empty state */}
        {phase === 'idle' && results.length === 0 && (
          <div className="fade-in" style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', height: '100%', gap: 16,
            color: 'var(--txt3)', textAlign: 'center',
          }}>
            <div style={{ fontSize: 56 }}>🔎</div>
            <div style={{ fontSize: 18, fontWeight: 600, color: 'var(--txt2)' }}>Copy Detector</div>
            {mode === 'cross_repo' && (
              <div style={{ maxWidth: 480, fontSize: 13, lineHeight: 1.8, color: 'var(--txt3)' }}>
                Detects <strong style={{ color: 'var(--txt2)' }}>copied or vendored code</strong> between two repositories
                using token fingerprinting (the same algorithm as&nbsp;
                <code style={{ fontFamily: 'var(--mono)', color: 'var(--accent)' }}>copydetect</code>).
              </div>
            )}
            {mode === 'self_scan' && (
              <div style={{ maxWidth: 480, fontSize: 13, lineHeight: 1.8, color: 'var(--txt3)' }}>
                Finds <strong style={{ color: 'var(--txt2)' }}>semantically duplicated functions</strong> within one repo —
                the same logic written twice (renamed, lightly reworded) inside the same class or module —
                using local sentence-embedding similarity, no LLM call.
              </div>
            )}
            {mode === 'corpus' && (
              <div style={{ maxWidth: 480, fontSize: 13, lineHeight: 1.8, color: 'var(--txt3)' }}>
                Checks a repo against a <strong style={{ color: 'var(--txt2)' }}>growing corpus</strong> of previously
                indexed repos — finds which corpus repo(s) it's most similar to, function by function.
                Add a repo to grow the corpus, or search an existing corpus for matches.
              </div>
            )}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center', marginTop: 6 }}>
              {(mode === 'cross_repo'
                ? ['Fingerprint files', 'Detect overlaps', 'Show ... between sections', 'Side-by-side diff']
                : mode === 'self_scan'
                ? ['AST-parse functions', 'Class-scoped comparison', 'Semantic embeddings', 'No LLM cost']
                : ['FAISS vector index', 'Grows with usage', 'Cross-repo matching', 'No LLM cost']
              ).map(f => (
                <span key={f} style={{ fontSize: 11, padding: '4px 10px', background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 20, color: 'var(--txt2)' }}>
                  {f}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Corpus "add" action — simple status panel, no match cards */}
        {mode === 'corpus' && corpusAction === 'add' && (isRunning || isDone || phase === 'error') && (
          <div className="fade-in" style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', height: '100%', gap: 12, textAlign: 'center',
          }}>
            <div style={{ fontSize: 40 }}>{isRunning ? '⏳' : isDone ? '✅' : '⚠️'}</div>
            <div style={{ fontSize: 14, color: 'var(--txt2)' }}>
              {isRunning && 'Indexing repo into the corpus…'}
              {isDone && 'Repo added to the corpus.'}
              {phase === 'error' && 'Failed to add repo to corpus.'}
            </div>
          </div>
        )}

        {/* Running state — show results as they stream in (cross_repo / self_scan / corpus search) */}
        {!(mode === 'corpus' && corpusAction === 'add') && (isRunning || isDone || results.length > 0) && (
          <div className="fade-in">

            {/* Corpus repo-level summary ranking */}
            {mode === 'corpus' && corpusSummary.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt3)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
                  Most similar corpus repos
                </div>
                {corpusSummary.map(r => (
                  <div key={r.repo} style={{
                    display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px',
                    background: 'var(--bg2)', borderRadius: 6, marginBottom: 4, fontSize: 12,
                  }}>
                    <span style={{ color: 'var(--accent)', fontFamily: 'var(--mono)', flex: 1 }}>{r.repo}</span>
                    <span style={{ color: 'var(--txt3)' }}>{r.match_count} match{r.match_count !== 1 ? 'es' : ''}</span>
                    <span style={{ color: simColor(r.max_similarity), fontWeight: 600, minWidth: 40, textAlign: 'right' }}>
                      {Math.round(r.max_similarity * 100)}%
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* Summary header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
              <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--txt)' }}>
                {results.length}
              </div>
              <div style={{ fontSize: 13, color: 'var(--txt2)' }}>
                {mode === 'self_scan' ? 'duplicate' : 'match'}{results.length !== 1 ? 'es' : ''} found
                {isRunning && <span className="pulse" style={{ color: 'var(--accent)', marginLeft: 8 }}>● scanning…</span>}
                {isDone && <span style={{ color: 'var(--green)', marginLeft: 8 }}>✓ complete</span>}
              </div>

              {results.length > 0 && (
                <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, fontSize: 11, color: 'var(--txt3)' }}>
                  <span style={{ color: '#f87171' }}>■ high ≥80%</span>
                  <span style={{ color: '#fbbf24' }}>■ mid ≥50%</span>
                  <span style={{ color: '#34d399' }}>■ low &lt;50%</span>
                </div>
              )}
            </div>

            {/* Result cards */}
            {results.length === 0 && isDone && (
              <div style={{ padding: '40px 0', textAlign: 'center', color: 'var(--txt3)', fontSize: 13 }}>
                {mode === 'cross_repo' && `🎉 No copies detected above ${Math.round(minSim * 100)}% similarity.`}
                {mode === 'self_scan'  && `🎉 No duplicate functions found above ${Math.round(dupThreshold * 100)}% similarity.`}
                {mode === 'corpus'     && `🎉 No matches found in the corpus above ${Math.round(corpusThreshold * 100)}% similarity.`}
              </div>
            )}

            {mode === 'cross_repo' && detections.map((det, i) => <DetectionCard key={i} det={det} index={i} />)}
            {mode === 'self_scan'  && duplicates.map((dup, i) => <DuplicateCard key={i} dup={dup} index={i} />)}
            {mode === 'corpus'     && corpusMatches.map((m, i) => <CorpusMatchCard key={i} m={m} index={i} />)}
          </div>
        )}

        {/* Error state */}
        {phase === 'error' && (
          <div style={{ padding: '20px 0', color: 'var(--red)', fontSize: 13 }}>
            {log.filter(l => l.type === 'error').map(l => l.text).join('\n')}
          </div>
        )}
      </div>
    </div>
  )
}
