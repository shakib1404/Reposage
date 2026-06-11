import { useEffect, useRef } from 'react'

const TYPE_META = {
  explore:  { label:'explore',  bg:'rgba(93,142,255,0.12)',  border:'rgba(93,142,255,0.3)',  color:'#5d8eff' },
  exec:     { label:'exec',     bg:'rgba(52,211,153,0.10)',  border:'rgba(52,211,153,0.3)',  color:'#34d399' },
  feedback: { label:'feedback', bg:'rgba(251,191,36,0.10)',  border:'rgba(251,191,36,0.3)',  color:'#fbbf24' },
  error:    { label:'error',    bg:'rgba(248,113,113,0.10)', border:'rgba(248,113,113,0.3)', color:'#f87171' },
  context:  { label:'context',  bg:'rgba(167,139,250,0.10)', border:'rgba(167,139,250,0.3)', color:'#a78bfa' },
  done:     { label:'done',     bg:'rgba(52,211,153,0.12)',  border:'rgba(52,211,153,0.4)',  color:'#34d399' },
}

export default function LoopFeed({ events = [] }) {
  const bottomRef = useRef()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [events.length])

  return (
    <div>
      {events.map((ev, i) => <EventCard key={i} ev={ev} />)}
      <div ref={bottomRef} />
    </div>
  )
}

function EventCard({ ev }) {
  const meta = TYPE_META[ev.type] || TYPE_META.explore

  return (
    <div className="fade-in" style={{
      background: meta.bg,
      border: `1px solid ${meta.border}`,
      borderRadius: 10,
      padding: '12px 14px',
      marginBottom: 8,
    }}>
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom: ev.body ? 6 : 0 }}>
        <span style={{ fontSize:10, fontWeight:600, padding:'2px 8px', borderRadius:12, background: meta.bg, border:`1px solid ${meta.border}`, color: meta.color, textTransform:'uppercase', letterSpacing:'0.06em' }}>
          {meta.label}
        </span>
        <span style={{ fontSize:13, fontWeight:500 }}>{ev.title}</span>
        {ev.tool && (
          <span style={{ marginLeft:'auto', fontSize:10, color:'var(--txt3)', fontFamily:'var(--mono)' }}>{ev.tool}</span>
        )}
      </div>

      {ev.body && (
        <p style={{ fontSize:12, color:'var(--txt2)', lineHeight:1.6, margin:'4px 0' }}>{ev.body}</p>
      )}

      {ev.code && (
        <pre style={{
          fontFamily:'var(--mono)', fontSize:11, background:'var(--bg)', border:'1px solid var(--border)',
          borderRadius:6, padding:'8px 10px', marginTop:8, overflowX:'auto', color:'var(--txt2)',
          lineHeight:1.7, whiteSpace:'pre-wrap', wordBreak:'break-all',
        }}>
          <code>{ev.code}</code>
        </pre>
      )}
    </div>
  )
}
