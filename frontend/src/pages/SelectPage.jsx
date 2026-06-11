import { useState } from 'react'

export default function SelectPage({ task, repos, selectedRepo, setSelectedRepo, analysis, setAnalysis, unlock, go }) {
  const [sel, setSel] = useState(selectedRepo)

  const pick = (r) => { setSel(r); setSelectedRepo(r) }

  const proceed = () => {
    if (!sel) return
    unlock('analyze')
    go('analyze')
  }

  return (
    <div style={{ maxWidth: 680, margin: '0 auto', padding: '28px 24px' }} className="fade-in">
      <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 4, letterSpacing: '-0.01em' }}>Select a repository</h2>
      <p style={{ color: 'var(--txt2)', fontSize: 13, marginBottom: 20 }}>Task: <em style={{ color: 'var(--txt)' }}>{task}</em></p>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 20 }}>
        {repos.map((r) => {
          const isSel = sel?.full_name === r.full_name
          return (
            <div key={r.full_name} onClick={() => pick(r)} style={{
              background: isSel ? 'rgba(93,142,255,0.07)' : 'var(--bg2)',
              border: `1px solid ${isSel ? 'var(--accent)' : 'var(--border)'}`,
              borderRadius: 'var(--radius-lg)', padding: '16px 18px', cursor: 'pointer',
              transition: 'all 0.15s', position: 'relative',
            }}>
              {isSel && (
                <div style={{ position:'absolute',top:14,right:14,width:22,height:22,borderRadius:'50%',background:'var(--accent)',display:'flex',alignItems:'center',justifyContent:'center' }}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                </div>
              )}
              <div style={{ display:'flex', alignItems:'center', gap:12, marginBottom:10 }}>
                <div style={{ fontSize: 28 }}>{r.icon || '📦'}</div>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>{r.name}</div>
                  <div style={{ color: 'var(--txt2)', fontSize: 12 }}>{r.owner} / {r.full_name}</div>
                </div>
              </div>
              <div style={{ display:'flex', gap:8, marginBottom:10, flexWrap:'wrap' }}>
                <Badge>⭐ {r.stars || '?'}</Badge>
                <Badge>{r.language || 'Python'}</Badge>
                <Badge color="green">Score: {typeof r.score === 'number' ? r.score.toFixed(1) : r.score}</Badge>
              </div>
              <div style={{ fontSize: 13, color: 'var(--txt2)', lineHeight: 1.55 }}>{r.description}</div>
              <a href={`https://github.com/${r.full_name}`} target="_blank" rel="noreferrer"
                onClick={e => e.stopPropagation()}
                style={{ display:'inline-block', marginTop:8, fontSize:12, color:'var(--accent)', textDecoration:'none' }}>
                View on GitHub ↗
              </a>
            </div>
          )
        })}
      </div>

      <div style={{ display:'flex', justifyContent:'flex-end' }}>
        <button onClick={proceed} disabled={!sel} style={{
          padding:'10px 20px', border:'none', borderRadius:'var(--radius)',
          background: sel ? 'var(--accent)' : 'var(--bg3)', color: sel ? 'white' : 'var(--txt3)',
          fontFamily:'var(--font)', fontSize:13, fontWeight:500,
          cursor: sel ? 'pointer' : 'not-allowed', display:'flex', alignItems:'center', gap:6,
        }}>
          Analyze selected repo →
        </button>
      </div>
    </div>
  )
}

function Badge({ children, color }) {
  const bg = color === 'green' ? 'var(--green-dim)' : 'var(--bg3)'
  const cl = color === 'green' ? 'var(--green)' : 'var(--txt2)'
  return <span style={{ padding:'3px 10px', background:bg, color:cl, borderRadius:20, fontSize:12 }}>{children}</span>
}
