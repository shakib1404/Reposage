import { useState } from 'react'
import { searchRepos } from '../api'

export default function SelectPage({
  task, setTask, repos, setRepos, suggestions, setSuggestions,
  selectedRepo, setSelectedRepo, unlock, go,
}) {
  const [sel, setSel]           = useState(selectedRepo)
  const [refining, setRefining] = useState('')   // chip label currently loading
  const [error, setError]       = useState('')

  const pick = (r) => { setSel(r); setSelectedRepo(r) }

  const proceed = () => {
    if (!sel) return
    unlock('analyze')
    go('analyze')
  }

  // Clicking a "refine your search" chip re-runs the whole search with that
  // narrower phrase and swaps the grid in place — the task is updated too, so
  // every downstream step (analyze/execute) uses the refined intent.
  const refine = async (phrase) => {
    if (refining) return
    setRefining(phrase)
    setError('')
    try {
      const data = await searchRepos(phrase)
      setTask(phrase)
      setRepos(data.repos)
      setSuggestions?.(data.suggestions || [])
      setSel(null)
      setSelectedRepo(null)
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } catch (e) {
      setError(e.message)
    } finally {
      setRefining('')
    }
  }

  return (
    <div style={{ maxWidth: 1080, margin: '0 auto', padding: '28px 24px' }} className="fade-in">
      <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 4, letterSpacing: '-0.01em' }}>
        Select a repository
      </h2>
      <p style={{ color: 'var(--txt2)', fontSize: 13, marginBottom: 20 }}>
        Task: <em style={{ color: 'var(--txt)' }}>{task}</em>
      </p>

      {/* ── Refine your search ─────────────────────────────────────────── */}
      {suggestions?.length > 0 && (
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div style={{
            fontSize: 11, letterSpacing: '0.08em', textTransform: 'uppercase',
            color: 'var(--txt3)', marginBottom: 10,
          }}>
            Refine your search
          </div>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'center', flexWrap: 'wrap' }}>
            {suggestions.map(s => (
              <button key={s} onClick={() => refine(s)} disabled={!!refining}
                style={{
                  padding: '7px 16px', borderRadius: 20,
                  border: '1px solid var(--border)',
                  background: refining === s ? 'var(--accent)' : 'var(--bg2)',
                  color: refining === s ? '#fff' : 'var(--txt)',
                  fontFamily: 'var(--font)', fontSize: 13,
                  cursor: refining ? 'wait' : 'pointer',
                  opacity: refining && refining !== s ? 0.5 : 1,
                  transition: 'all 0.15s',
                }}>
                {refining === s ? 'Searching…' : s}
              </button>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div style={{
          background: 'var(--red-dim)', color: 'var(--red)', padding: '10px 14px',
          borderRadius: 'var(--radius)', fontSize: 13, marginBottom: 16,
        }}>{error}</div>
      )}

      <div style={{ fontSize: 13, color: 'var(--txt2)', marginBottom: 12 }}>
        Showing {repos.length} result{repos.length === 1 ? '' : 's'}
      </div>

      {/* ── Result grid ────────────────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
        gap: 14, marginBottom: 24,
      }}>
        {repos.map((r) => {
          const isSel = sel?.full_name === r.full_name
          return (
            <div key={r.full_name} onClick={() => pick(r)} style={{
              background: isSel ? 'rgba(93,142,255,0.07)' : 'var(--bg2)',
              border: `1px solid ${isSel ? 'var(--accent)' : 'var(--border)'}`,
              borderRadius: 'var(--radius-lg)', padding: '16px 18px', cursor: 'pointer',
              transition: 'all 0.15s', position: 'relative',
              display: 'flex', flexDirection: 'column',
            }}>
              {isSel && (
                <div style={{
                  position: 'absolute', top: 12, right: 12, width: 20, height: 20,
                  borderRadius: '50%', background: 'var(--accent)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="white"
                       strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                </div>
              )}

              {/* Owner avatar + repo name */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10, paddingRight: 24 }}>
                <img src={r.avatar} alt="" width={32} height={32} loading="lazy"
                  onError={e => { e.currentTarget.style.visibility = 'hidden' }}
                  style={{ borderRadius: '50%', flexShrink: 0, background: 'var(--bg3)' }} />
                <a href={`https://github.com/${r.full_name}`} target="_blank" rel="noreferrer"
                  onClick={e => e.stopPropagation()}
                  title={r.full_name}
                  style={{
                    fontWeight: 600, fontSize: 14, color: 'var(--accent)',
                    textDecoration: 'none', overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                  {r.name}
                </a>
              </div>

              {/* Description */}
              <div style={{
                fontSize: 13, color: 'var(--txt2)', lineHeight: 1.5,
                marginBottom: 12, flex: 1,
                display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
              }}>
                {r.description || 'No description provided.'}
              </div>

              {/* Stats row */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap',
                fontSize: 12, color: 'var(--txt3)',
              }}>
                <span title="Stars">☆ {r.stars ?? '?'}</span>
                <span title="Forks">⑂ {r.forks ?? '?'}</span>
                {r.runnable != null && (
                  <span title="Runnability — how likely RepoSage can clone, install and execute this repo"
                    style={{ color: r.runnable >= 7 ? 'var(--green)' : 'var(--txt3)' }}>
                    ▶ {r.runnable}
                  </span>
                )}
                {(r.run_tags || []).includes('stale') && (
                  <span title="No commits in 5+ years" style={{ color: 'var(--txt3)' }}>· stale</span>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button onClick={proceed} disabled={!sel} style={{
          padding: '10px 20px', border: 'none', borderRadius: 'var(--radius)',
          background: sel ? 'var(--accent)' : 'var(--bg3)',
          color: sel ? 'white' : 'var(--txt3)',
          fontFamily: 'var(--font)', fontSize: 13, fontWeight: 500,
          cursor: sel ? 'pointer' : 'not-allowed',
          display: 'flex', alignItems: 'center', gap: 6,
        }}>
          Analyze selected repo →
        </button>
      </div>
    </div>
  )
}
