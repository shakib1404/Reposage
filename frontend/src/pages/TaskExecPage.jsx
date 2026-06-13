import { useState, useRef, useEffect } from 'react'
import { streamTaskExec } from '../api'

// Parse "owner/repo" or full GitHub URL → "owner/repo"
function parseRepo(raw) {
  const s = raw.trim()
  const m = s.match(/github\.com[/:]([\w.-]+\/[\w.-]+?)(?:\.git)?(?:\/.*)?$/)
  if (m) return m[1]
  if (/^[\w.-]+\/[\w.-]+$/.test(s)) return s
  return s
}

export default function TaskExecPage({ task: defaultTask, selectedRepo, jobId, inputFiles }) {
  const [task,    setTask]    = useState(defaultTask || '')
  const [repoInput, setRepoInput] = useState(selectedRepo?.full_name || '')
  const [phase,   setPhase]   = useState('idle')   // idle | running | done | error
  const [lines,   setLines]   = useState([])
  const [rc,      setRc]      = useState(null)
  const cleanupRef = useRef(null)
  const logEndRef  = useRef(null)

  // Keep repo input pre-filled when workflow repo changes
  useEffect(() => {
    if (selectedRepo?.full_name && !repoInput)
      setRepoInput(selectedRepo.full_name)
  }, [selectedRepo])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [lines])

  useEffect(() => {
    return () => cleanupRef.current?.()
  }, [])

  const resolvedRepo = parseRepo(repoInput)
  const canRun = task.trim() && resolvedRepo.includes('/')

  function start() {
    if (!canRun) return
    setPhase('running')
    setLines([])
    setRc(null)

    const inputFile = (inputFiles || [])[0] || ''
    cleanupRef.current = streamTaskExec(
      task.trim(),
      resolvedRepo,
      jobId || '',
      inputFile,
      (ev) => {
        if (ev.type === 'output') {
          setLines(prev => [...prev, ev.line])
        } else if (ev.type === 'done') {
          setRc(ev.returncode)
          setPhase(ev.returncode === 0 ? 'done' : 'error')
        } else if (ev.type === 'error') {
          // Also print the error into the terminal so it's always visible
          setLines(prev => [...prev, `✕  ${ev.message || ev.body || 'Unknown error'}`])
          setPhase('error')
        } else {
          // Catch-all: dump anything unexpected into the terminal
          const raw = JSON.stringify(ev)
          setLines(prev => [...prev, raw])
        }
      }
    )
  }

  function stop() {
    cleanupRef.current?.()
    setPhase('idle')
    setLines(prev => [...prev, '— stopped by user —'])
  }

  const success = phase === 'done'

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 97px)', overflow: 'hidden' }}>

      {/* ── Left panel ────────────────────────────────────────────────── */}
      <div style={{ width: 300, flexShrink: 0, borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '16px 16px 14px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 14 }}>RepoTask Executor</div>

          {/* Repository input */}
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--txt3)', marginBottom: 4 }}>Repository</div>
            <input
              value={repoInput}
              onChange={e => setRepoInput(e.target.value)}
              placeholder="owner/repo or GitHub URL"
              style={{
                width: '100%', boxSizing: 'border-box',
                padding: '7px 10px', fontSize: 12,
                background: 'var(--bg3)', border: '1px solid var(--border2)',
                borderRadius: 'var(--radius)', color: 'var(--txt)',
                fontFamily: 'var(--mono)', outline: 'none',
              }}
            />
            {repoInput && resolvedRepo.includes('/') && (
              <div style={{ fontSize: 10, color: 'var(--green)', marginTop: 3 }}>
                ✓ {resolvedRepo}
              </div>
            )}
            {repoInput && !resolvedRepo.includes('/') && (
              <div style={{ fontSize: 10, color: 'var(--yellow)', marginTop: 3 }}>
                Enter as owner/repo
              </div>
            )}
          </div>

          {/* Task input */}
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--txt3)', marginBottom: 4 }}>Task description</div>
            <textarea
              value={task}
              onChange={e => setTask(e.target.value)}
              placeholder="Describe what to run or analyse…"
              rows={4}
              style={{
                width: '100%', boxSizing: 'border-box',
                padding: '7px 10px', fontSize: 12, lineHeight: 1.5,
                background: 'var(--bg3)', border: '1px solid var(--border2)',
                borderRadius: 'var(--radius)', color: 'var(--txt)',
                fontFamily: 'var(--font)', resize: 'vertical',
                outline: 'none',
              }}
            />
          </div>

          {phase === 'running' ? (
            <button
              onClick={stop}
              style={{
                width: '100%', padding: '9px 0',
                background: 'transparent', border: '1px solid var(--red)',
                borderRadius: 'var(--radius)', color: 'var(--red)',
                fontSize: 13, fontWeight: 500, cursor: 'pointer',
              }}
            >
              ■  Stop
            </button>
          ) : (
            <button
              onClick={start}
              disabled={!canRun}
              style={{
                width: '100%', padding: '9px 0',
                background: canRun ? 'var(--accent)' : 'var(--bg3)',
                border: canRun ? 'none' : '1px solid var(--border)',
                borderRadius: 'var(--radius)', color: canRun ? 'white' : 'var(--txt3)',
                fontSize: 13, fontWeight: 500,
                cursor: canRun ? 'pointer' : 'not-allowed',
              }}
            >
              {phase === 'idle' ? '▶  Run Task' : '↺  Re-run'}
            </button>
          )}
        </div>

        {/* Status panel */}
        <div style={{ padding: '12px 16px', flex: 1, overflowY: 'auto' }}>
          <div style={{ fontSize: 10, fontWeight: 500, color: 'var(--txt3)', marginBottom: 10, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
            Run info
          </div>

          {[
            { label: 'Repo',       value: resolvedRepo || '—' },
            { label: 'Input file', value: (inputFiles || [])[0] || 'none' },
            { label: 'Lines out',  value: lines.length },
          ].map(({ label, value }) => (
            <div key={label} style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 10, color: 'var(--txt3)', marginBottom: 2 }}>{label}</div>
              <div style={{ fontSize: 12, color: 'var(--txt)', wordBreak: 'break-all', fontFamily: label === 'Repo' ? 'var(--mono)' : 'inherit' }}>{String(value)}</div>
            </div>
          ))}

          {rc !== null && (
            <div style={{
              marginTop: 8, padding: '8px 10px', borderRadius: 'var(--radius)',
              background: success ? 'var(--green-dim)' : 'rgba(248,113,113,0.1)',
              border: `1px solid ${success ? 'var(--green)' : 'var(--red)'}`,
              fontSize: 12, fontWeight: 600,
              color: success ? 'var(--green)' : 'var(--red)',
            }}>
              {success ? '✅ Completed (exit 0)' : `❌ Failed (exit ${rc})`}
            </div>
          )}

          {phase === 'running' && (
            <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--accent)' }}>
              <span className="pulse">●</span> Running…
            </div>
          )}
        </div>
      </div>

      {/* ── Right panel: terminal ──────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

        {/* Terminal header */}
        <div style={{
          padding: '10px 16px',
          borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <div style={{ display: 'flex', gap: 5 }}>
            <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#f87171' }} />
            <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#fbbf24' }} />
            <div style={{ width: 10, height: 10, borderRadius: '50%', background: '#34d399' }} />
          </div>
          <span style={{ fontSize: 11, color: 'var(--txt3)', fontFamily: 'var(--mono)' }}>
            repository_agent — {resolvedRepo || 'no repo'}
          </span>
          {lines.length > 0 && (
            <button
              onClick={() => setLines([])}
              style={{
                marginLeft: 'auto', fontSize: 10, color: 'var(--txt3)',
                background: 'transparent', border: '1px solid var(--border)',
                borderRadius: 4, padding: '2px 8px', cursor: 'pointer',
              }}
            >
              Clear
            </button>
          )}
        </div>

        {/* Terminal body */}
        <div style={{
          flex: 1, overflowY: 'auto',
          background: 'var(--bg)',
          padding: '12px 16px',
          fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.65,
        }}>
          {lines.length === 0 && phase === 'idle' && (
            <div style={{ color: 'var(--txt3)', textAlign: 'center', marginTop: 60 }}>
              <div style={{ fontSize: 48, marginBottom: 12 }}>🤖</div>
              <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--txt2)', marginBottom: 8 }}>
                RepoTask Executor
              </div>
              <div style={{ fontSize: 12, maxWidth: 400, margin: '0 auto', lineHeight: 1.8 }}>
                Enter any GitHub repository and a task description.<br />
                The AI agent will autonomously run or analyse it.<br />
                <span style={{ color: 'var(--accent)' }}>No prior workflow step required.</span>
              </div>
            </div>
          )}

          {lines.map((line, i) => {
            const isError   = /error|traceback|exception|failed/i.test(line)
            const isSuccess = /success|complete|done|✓|✅/i.test(line)
            const isWarn    = /warning|warn/i.test(line)
            const color = isError   ? 'var(--red)'
                        : isSuccess ? 'var(--green)'
                        : isWarn    ? 'var(--yellow)'
                        : 'var(--txt2)'
            return (
              <div key={i} style={{ color, marginBottom: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {line}
              </div>
            )
          })}

          {phase === 'running' && (
            <span className="pulse" style={{ color: 'var(--accent)', fontSize: 14 }}>▌</span>
          )}

          <div ref={logEndRef} />
        </div>
      </div>
    </div>
  )
}
