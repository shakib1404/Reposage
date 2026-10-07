import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { listOutputs, getOutputUrl } from '../api'
import ScoreBar from '../components/ScoreBar'
import {
  RadarChart, PolarGrid, PolarAngleAxis,
  Radar, ResponsiveContainer, Tooltip,
} from 'recharts'

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg'])
const VIDEO_EXTS = new Set(['.mp4', '.avi', '.mov', '.webm'])

function ext(name) {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

function fileIcon(name) {
  const e = ext(name)
  if (IMAGE_EXTS.has(e))                              return '🖼️'
  if (VIDEO_EXTS.has(e))                              return '🎬'
  if (e === '.py')                                    return '🐍'
  if (e === '.sh')                                    return '📜'
  if (e === '.csv')                                   return '📊'
  if (e === '.json')                                  return '📋'
  if (['.pt', '.pth', '.onnx', '.h5', '.pkl'].includes(e)) return '🧠'
  if (e === '.txt' || e === '.log')                   return '📝'
  return '📄'
}

function fmtBytes(n) {
  if (n < 1024)            return n + ' B'
  if (n < 1024 * 1024)     return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}

function downloadText(filename, content, mime = 'text/plain') {
  const blob = new Blob([content], { type: mime })
  const url  = URL.createObjectURL(blob)
  const a    = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

// ── Copy-to-clipboard button ─────────────────────────────────────────────────
function CopyButton({ text, style = {} }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      // fallback
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    }
  }
  return (
    <button
      onClick={copy}
      style={{
        fontSize: 11, cursor: 'pointer',
        background: copied ? 'var(--green-dim)' : 'transparent',
        border: `1px solid ${copied ? 'var(--green)' : 'var(--border2)'}`,
        color: copied ? 'var(--green)' : 'var(--txt2)',
        borderRadius: 4, padding: '3px 10px', transition: 'all 0.2s',
        ...style,
      }}
    >
      {copied ? '✓ Copied' : '⎘ Copy'}
    </button>
  )
}

// ── Section heading ──────────────────────────────────────────────────────────
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

// ── Manual guide renderer (markdown-lite) ─────────────────────────────────────
function ManualGuide({ guide }) {
  if (!guide) return null
  const lines = guide.split('\n')
  return (
    <div style={{ fontSize: 13, lineHeight: 1.7, color: 'var(--txt)' }}>
      {lines.map((line, i) => {
        if (line.startsWith('## '))
          return <h3 key={i} style={{ fontSize: 15, fontWeight: 600, margin: '4px 0 8px', color: 'var(--red)' }}>{line.slice(3)}</h3>
        if (line.startsWith('### '))
          return <h4 key={i} style={{ fontSize: 13, fontWeight: 600, margin: '14px 0 4px', color: 'var(--txt)' }}>{line.slice(4)}</h4>
        if (line.startsWith('- '))
          return <div key={i} style={{ paddingLeft: 16, marginBottom: 3 }}>• {renderInline(line.slice(2))}</div>
        if (line === '```bash' || line === '```')
          return null
        if (line === '---' || line === '')
          return <div key={i} style={{ height: 6 }} />
        return <div key={i}>{renderInline(line)}</div>
      })}
    </div>
  )
}

function renderInline(text) {
  // Bold **...**
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**'))
      return <strong key={i}>{p.slice(2, -2)}</strong>
    if (p.startsWith('`') && p.endsWith('`'))
      return <code key={i} style={{ fontFamily: 'var(--mono)', fontSize: 11, background: 'var(--bg3)', padding: '1px 4px', borderRadius: 3 }}>{p.slice(1, -1)}</code>
    return p
  })
}

// ─────────────────────────────────────────────────────────────────────────────

export default function OutputPage({ task, selectedRepo, analysis, execResult, unlock, go }) {
  const [files, setFiles]           = useState([])
  const [loading, setLoading]       = useState(true)
  const [error, setError]           = useState('')
  const [preview, setPreview]       = useState(null)
  const [previewError, setPreviewError] = useState('')
  const [activeImg, setActiveImg]   = useState(null)
  const [retries, setRetries]       = useState(0)
  const [showScript, setShowScript] = useState(true)
  const [showGuide, setShowGuide]   = useState(true)

  const jobId        = execResult?.job_id    || ''
  const metrics      = execResult?.metrics   || {}
  const modules      = analysis?.modules     || []
  const success      = (execResult?.returncode ?? 0) === 0
  const termOutput   = execResult?.output    || ''
  const runScript    = execResult?.run_script || ''
  const manualGuide  = execResult?.manual_guide || ''
  const fixJournal   = execResult?.fix_journal  || []

  useEffect(() => { unlock('test') }, [])

  useEffect(() => {
    if (!jobId) { setLoading(false); return }
    setLoading(true)
    setError('')
    listOutputs(jobId)
      .then(data => {
        const fs = data.files || []
        setFiles(fs)
        setLoading(false)
        const script = fs.find(f => f.name === 'run_final.sh')
        if (script && !runScript) fetchPreview(jobId, script.name)
      })
      .catch(e => { setError(e.message); setLoading(false) })
  }, [jobId, retries])

  const imageFiles = files.filter(f => IMAGE_EXTS.has(ext(f.name)))
  const otherFiles = files.filter(
    f => !IMAGE_EXTS.has(ext(f.name)) && f.name !== 'run_final.sh')
  // run_final.sh has its own card above, so it is filtered out of the list.
  // Count what is actually listed under THIS heading: images are hoisted into
  // their own card higher up, so including them made the header read
  // "Output files (3)" above two rows the moment a run produced an image.
  // shownCount stays the total — it decides whether there is anything at all
  // to show, and "no output files yet" would be wrong when an image exists.
  const listedCount = otherFiles.length
  const shownCount  = imageFiles.length + otherFiles.length

  function fetchPreview(jid, name) {
    setPreviewError('')
    fetch(getOutputUrl(jid, name))
      .then(r => {
        if (!r.ok) throw new Error(`${r.status}`)
        return r.text()
      })
      .then(text => {
        setPreview({ name, content: text })
        if (!text.trim()) setPreviewError(`"${name}" is empty.`)
      })
      .catch(err => {
        setPreview({ name, content: '' })
        setPreviewError(`Could not load "${name}": ${err.message}`)
      })
  }

  // ── Hero card ──────────────────────────────────────────────────────────────
  const heroColor  = success ? 'var(--green)'    : 'var(--red)'
  const heroBg     = success ? 'var(--green-dim)' : 'color-mix(in srgb, var(--danger, #ef4444) 8%, transparent)'
  const heroBorder = success ? 'var(--green)'    : 'var(--red)'
  const heroIcon   = success ? '✅' : '❌'
  const heroTitle  = success ? 'Task completed'  : 'Execution failed'

  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '24px 24px' }} className="fade-in">

      {/* ── Hero ─────────────────────────────────────────────────────────── */}
      <div style={{
        background: heroBg,
        border: `1px solid ${heroBorder}`,
        borderRadius: 'var(--radius-lg)',
        padding: '20px 24px',
        marginBottom: 16,
        display: 'flex', alignItems: 'center', gap: 16,
      }}>
        <div style={{ fontSize: 40 }}>{heroIcon}</div>
        <div style={{ flex: 1 }}>
          <div style={{
            fontWeight: 600, fontSize: 17,
            marginBottom: 4, color: heroColor,
          }}>
            {heroTitle}
          </div>
          <div style={{ fontSize: 13, color: 'var(--txt2)', lineHeight: 1.55 }}>
            {execResult?.summary
              || `${success ? 'Completed' : 'Attempted'} '${task}' using ${selectedRepo?.full_name}.`}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0, flexWrap: 'wrap' }}>
          {success && (
            <button onClick={() => go('test')}
              style={{ padding: '7px 13px', border: '1px solid var(--accent)', borderRadius: 'var(--radius)', background: 'var(--accent-dim)', color: 'var(--accent)', fontSize: 12, cursor: 'pointer' }}>
              Run Audit →
            </button>
          )}
          <button onClick={() => go('search')}
            style={{ padding: '7px 13px', border: '1px solid var(--border2)', borderRadius: 'var(--radius)', background: 'transparent', color: 'var(--txt)', fontSize: 12, cursor: 'pointer' }}>
            New task ↩
          </button>
        </div>
      </div>

      {/* ── No job_id warning ────────────────────────────────────────────── */}
      {!jobId && (
        <div style={{
          background: 'var(--yellow-dim)',
          border: '1px solid var(--yellow)',
          borderRadius: 'var(--radius-lg)',
          padding: '12px 18px', marginBottom: 12,
          fontSize: 13, color: 'var(--yellow)',
        }}>
          ⚠ No job ID — go back to <strong>Execute</strong> to generate outputs.
        </div>
      )}

      {/* ── Run script  (always shown, prominent) ────────────────────────── */}
      {runScript && (
        <div style={{
          background: 'var(--bg2)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '14px 18px', marginBottom: 12,
        }}>
          <div style={{
            display: 'flex', alignItems: 'center',
            justifyContent: 'space-between', marginBottom: 10,
          }}>
            <SectionLabel>📜 Run script (copy & paste to terminal)</SectionLabel>
            <div style={{ display: 'flex', gap: 8 }}>
              <CopyButton text={runScript} />
              <button
                onClick={() => downloadText('run_final.sh', runScript)}
                style={{ fontSize: 11, color: 'var(--green)', background: 'transparent', border: '1px solid var(--green)', borderRadius: 4, padding: '3px 10px', cursor: 'pointer' }}
              >
                ↓ Download
              </button>
              <button
                onClick={() => setShowScript(s => !s)}
                style={{ fontSize: 11, color: 'var(--txt3)', background: 'transparent', border: '1px solid var(--border)', borderRadius: 4, padding: '3px 10px', cursor: 'pointer' }}
              >
                {showScript ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>
          {showScript && (
            <pre style={{
              fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.6,
              overflowX: 'auto', maxHeight: 480, margin: 0,
              color: 'var(--txt)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              background: 'var(--bg3)', padding: '12px 14px', borderRadius: 'var(--radius)',
            }}>
              {runScript}
            </pre>
          )}
        </div>
      )}

      {/* ── Manual guide  (only when failed) ────────────────────────────── */}
      {!success && manualGuide && (
        <div style={{
          background: 'color-mix(in srgb, var(--danger, #ef4444) 6%, transparent)',
          border: '1px solid var(--red)',
          borderRadius: 'var(--radius-lg)',
          padding: '16px 20px', marginBottom: 12,
        }}>
          <div style={{
            display: 'flex', alignItems: 'center',
            justifyContent: 'space-between', marginBottom: 12,
          }}>
            <SectionLabel>🛠 What to do manually</SectionLabel>
            <button
              onClick={() => setShowGuide(s => !s)}
              style={{ fontSize: 11, color: 'var(--txt3)', background: 'transparent', border: '1px solid var(--border)', borderRadius: 4, padding: '3px 10px', cursor: 'pointer' }}
            >
              {showGuide ? 'Hide' : 'Show'}
            </button>
          </div>
          {showGuide && <ManualGuide guide={manualGuide} />}
        </div>
      )}

      {/* ── Fix journal (when failed) ─────────────────────────────────────── */}
      {!success && fixJournal.length > 0 && (
        <div style={{
          background: 'var(--bg2)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '14px 18px', marginBottom: 12,
        }}>
          <SectionLabel>🔧 Fix attempts ({fixJournal.length})</SectionLabel>
          {fixJournal.map((entry, i) => (
            <div key={i} style={{
              display: 'flex', gap: 10, marginBottom: 6,
              fontSize: 12, color: 'var(--txt2)',
              borderBottom: i < fixJournal.length - 1 ? '1px solid var(--border)' : 'none',
              paddingBottom: i < fixJournal.length - 1 ? 6 : 0,
            }}>
              <span style={{
                fontFamily: 'var(--mono)', fontSize: 11,
                background: entry.result === 'autofix_applied'
                  ? 'var(--accent-dim)' : 'var(--bg3)',
                color: entry.result === 'autofix_applied'
                  ? 'var(--accent)' : 'var(--txt2)',
                padding: '1px 6px', borderRadius: 3,
                flexShrink: 0,
              }}>
                #{entry.attempt} {entry.fix_type}
              </span>
              <span style={{ flex: 1 }}>
                {entry.root_cause || entry.explanation || ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* ── Image outputs ─────────────────────────────────────────────────── */}
      {imageFiles.length > 0 && (
        <div style={{
          background: 'var(--bg2)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '16px 18px', marginBottom: 12,
        }}>
          <SectionLabel>Output images</SectionLabel>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
            {imageFiles.map(f => {
              const url = getOutputUrl(jobId, f.name)
              return (
                <div key={f.name} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
                  <img
                    src={url} alt={f.name}
                    onClick={() => setActiveImg(url)}
                    style={{
                      width: 200, height: 150, objectFit: 'cover',
                      borderRadius: 8, border: '1px solid var(--border)',
                      cursor: 'zoom-in', background: 'var(--bg3)',
                    }}
                    onError={e => { e.target.style.display = 'none' }}
                  />
                  <div style={{
                    fontSize: 11, color: 'var(--txt2)',
                    maxWidth: 200, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {f.name}
                  </div>
                  <a href={getOutputUrl(jobId, f.name)} download={f.name}
                    style={{ fontSize: 11, color: 'var(--accent)', textDecoration: 'none' }}>
                    ↓ Download
                  </a>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Lightbox — portalled to <body>: the page root's fade-in animates
          `transform`, which makes it the containing block for position:fixed
          children, so an in-place backdrop only covered the 960px column. */}
      {activeImg && createPortal(
        <div
          onClick={() => setActiveImg(null)}
          style={{
            position: 'fixed', inset: 0,
            background: 'rgba(0,0,0,0.88)', zIndex: 1000,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            cursor: 'zoom-out',
          }}
        >
          <img src={activeImg} alt="preview"
            style={{ maxWidth: '90vw', maxHeight: '90vh', borderRadius: 8 }} />
        </div>,
        document.body,
      )}

      {/* ── Output files ─────────────────────────────────────────────────── */}
      <div style={{
        background: 'var(--bg2)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        padding: '14px 18px', marginBottom: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <SectionLabel>
            Output files
            {loading ? ' (loading…)' : ` (${listedCount})`}
          </SectionLabel>
          <div style={{ flex: 1 }} />
          {jobId && (
            <button
              onClick={() => setRetries(r => r + 1)}
              style={{
                fontSize: 11, color: 'var(--txt3)',
                background: 'transparent', border: '1px solid var(--border)',
                borderRadius: 4, padding: '2px 8px', cursor: 'pointer',
              }}
            >
              ↻ Refresh
            </button>
          )}
        </div>

        {error && (
          <div style={{ fontSize: 12, color: 'var(--red)', marginBottom: 8 }}>
            Error: {error}
          </div>
        )}
        {!loading && shownCount === 0 && !error && jobId && (
          <div style={{ fontSize: 13, color: 'var(--txt3)', padding: '6px 0' }}>
            No output files yet. Click <strong>↻ Refresh</strong> if execution just finished.
          </div>
        )}

        {otherFiles.map(f => {
          const url      = getOutputUrl(jobId, f.name)
          const viewable = ['.py', '.sh', '.txt', '.log', '.json', '.csv', '.md'].includes(ext(f.name))
          return (
            <div key={f.name} style={{
              display: 'flex', alignItems: 'center', gap: 10,
              marginBottom: 7, fontSize: 13,
            }}>
              <span style={{ fontSize: 16, flexShrink: 0 }}>{fileIcon(f.name)}</span>
              <code style={{
                fontFamily: 'var(--mono)', fontSize: 12, flex: 1,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>
                {f.name}
              </code>
              <span style={{ fontSize: 11, color: 'var(--txt3)', flexShrink: 0 }}>
                {fmtBytes(f.size_bytes)}
              </span>
              {viewable && (
                <button
                  onClick={() => fetchPreview(jobId, f.name)}
                  style={{
                    fontSize: 11, color: 'var(--accent)',
                    background: 'transparent', border: '1px solid var(--border2)',
                    borderRadius: 4, padding: '2px 8px', cursor: 'pointer', flexShrink: 0,
                  }}
                >
                  View
                </button>
              )}
              <a href={url} download={f.name}
                style={{
                  fontSize: 11, color: 'var(--green)', textDecoration: 'none',
                  border: '1px solid var(--green)',
                  borderRadius: 4, padding: '2px 10px', flexShrink: 0,
                }}>
                ↓
              </a>
            </div>
          )
        })}
      </div>

      {/* ── File preview panel ───────────────────────────────────────────── */}
      {preview && (
        <div style={{
          background: 'var(--bg2)',
          border: `1px solid ${previewError ? 'var(--yellow)' : 'var(--border)'}`,
          borderRadius: 'var(--radius-lg)',
          padding: '14px 18px', marginBottom: 12,
        }}>
          <div style={{
            display: 'flex', alignItems: 'center',
            justifyContent: 'space-between', marginBottom: 10,
          }}>
            <div style={{
              fontSize: 11, fontWeight: 500, color: 'var(--txt2)',
              textTransform: 'uppercase', letterSpacing: '0.06em',
            }}>
              {fileIcon(preview.name)} {preview.name}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              {preview.content && (
                <>
                  <CopyButton text={preview.content} />
                  <button
                    onClick={() => downloadText(preview.name, preview.content)}
                    style={{
                      fontSize: 11, color: 'var(--green)',
                      background: 'transparent', border: '1px solid var(--green)',
                      borderRadius: 4, padding: '3px 10px', cursor: 'pointer',
                    }}
                  >
                    ↓ Download
                  </button>
                </>
              )}
              <button
                onClick={() => { setPreview(null); setPreviewError('') }}
                style={{
                  fontSize: 11, color: 'var(--txt3)',
                  background: 'transparent', border: '1px solid var(--border)',
                  borderRadius: 4, padding: '3px 10px', cursor: 'pointer',
                }}
              >
                Close
              </button>
            </div>
          </div>
          {previewError && (
            <div style={{
              fontSize: 12, color: 'var(--yellow)',
              background: 'var(--yellow-dim)',
              border: '1px solid var(--yellow)',
              borderRadius: 6, padding: '8px 12px', marginBottom: 10,
            }}>
              ⚠ {previewError}
            </div>
          )}
          {preview.content && (
            <pre style={{
              fontFamily: 'var(--mono)', fontSize: 12, lineHeight: 1.6,
              overflowX: 'auto', maxHeight: 420, margin: 0,
              color: 'var(--txt)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              {preview.content}
            </pre>
          )}
        </div>
      )}

      {/* ── Terminal output ───────────────────────────────────────────────── */}
      {termOutput && (
        <div style={{
          background: 'var(--bg2)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '14px 18px', marginBottom: 12,
        }}>
          <div style={{
            display: 'flex', alignItems: 'center',
            justifyContent: 'space-between', marginBottom: 10,
          }}>
            <SectionLabel>📟 Terminal output</SectionLabel>
            <div style={{ display: 'flex', gap: 8 }}>
              <CopyButton text={termOutput} />
              <button
                onClick={() => downloadText('execution_output.txt', termOutput)}
                style={{
                  fontSize: 11, color: 'var(--green)',
                  background: 'transparent', border: '1px solid var(--green)',
                  borderRadius: 4, padding: '3px 10px', cursor: 'pointer',
                }}
              >
                ↓ Download
              </button>
            </div>
          </div>
          <pre style={{
            fontFamily: 'var(--mono)', fontSize: 11, lineHeight: 1.6,
            overflowX: 'auto', maxHeight: 280, margin: 0,
            color: 'var(--txt2)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>
            {termOutput}
          </pre>
        </div>
      )}

      {/* ── Stats ─────────────────────────────────────────────────────────── */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)',
        gap: 8, marginBottom: 12,
      }}>
        {[
          { label: 'LLM calls',   value: metrics.calls  ?? '—', note: 'minimized' },
          { label: 'Tokens',      value: metrics.tokens ? (metrics.tokens / 1000).toFixed(1) + 'k' : '—', note: 'used' },
          { label: 'Iterations',  value: execResult?.iterations ?? metrics.iters ?? '—', note: 'loop cycles' },
          { label: 'Elapsed',     value: execResult?.elapsed_s  ? execResult.elapsed_s.toFixed(1) + 's' : '—', note: 'total time' },
        ].map(m => (
          <div key={m.label} style={{
            background: 'var(--bg2)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius)', padding: '12px 14px', textAlign: 'center',
          }}>
            <div style={{ fontSize: 21, fontWeight: 600 }}>{m.value}</div>
            <div style={{ fontSize: 11, color: 'var(--txt2)', marginTop: 2 }}>{m.label}</div>
            <div style={{ fontSize: 10, color: 'var(--txt3)' }}>{m.note}</div>
          </div>
        ))}
      </div>

      {/* ── Module ranking ────────────────────────────────────────────────── */}
      {modules.length > 0 && (
        <div style={{
          background: 'var(--bg2)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)', padding: '14px 18px', marginBottom: 12,
        }}>
          <SectionLabel>Module importance ranking</SectionLabel>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 24px' }}>
            {modules.slice(0, 10).map((m, i) => (
              <div key={m.name} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                {m.is_notebook && <span style={{ fontSize: 12 }}>📓</span>}
                <div style={{ flex: 1 }}>
                  <ScoreBar
                    name={m.name}
                    score={typeof m.score === 'number' ? m.score : 8.5 - i * 0.4}
                    index={i}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Radar chart ───────────────────────────────────────────────────── */}
      <div style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', padding: '14px 18px',
      }}>
        <SectionLabel>Module scoring features</SectionLabel>
        <ResponsiveContainer width="100%" height={180}>
          <RadarChart data={[
            { feature: 'Dependency', value: 8.5 },
            { feature: 'Complexity', value: 7.2 },
            { feature: 'Usage',      value: 9.1 },
            { feature: 'Semantic',   value: 8.0 },
            { feature: 'Doc',        value: 6.8 },
            { feature: 'Git',        value: 7.5 },
          ]} margin={{ top: 10, right: 20, bottom: 10, left: 20 }}>
            <PolarGrid stroke="var(--border2)" />
            <PolarAngleAxis dataKey="feature" tick={{ fill: 'var(--txt2)', fontSize: 11 }} />
            <Radar dataKey="value" stroke="var(--accent)" fill="var(--accent)"
              fillOpacity={0.18} strokeWidth={1.5} />
            <Tooltip contentStyle={{
              background: 'var(--bg2)', border: '1px solid var(--border)',
              borderRadius: 8, fontSize: 12,
            }} />
          </RadarChart>
        </ResponsiveContainer>
      </div>

    </div>
  )
}
