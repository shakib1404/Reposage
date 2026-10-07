import { useEffect, useRef } from 'react'

const TYPE_META = {
  explore:  { label:'explore',  bg:'var(--accent-dim)',  border:'color-mix(in srgb, var(--accent) 30%, transparent)',  color:'var(--accent)' },
  exec:     { label:'exec',     bg:'color-mix(in srgb, var(--green) 10%, transparent)',  border:'color-mix(in srgb, var(--green) 30%, transparent)',  color:'var(--green)' },
  feedback: { label:'feedback', bg:'color-mix(in srgb, var(--yellow) 10%, transparent)',  border:'color-mix(in srgb, var(--yellow) 30%, transparent)',  color:'var(--yellow)' },
  error:    { label:'error',    bg:'color-mix(in srgb, var(--red) 10%, transparent)', border:'color-mix(in srgb, var(--red) 30%, transparent)', color:'var(--red)' },
  context:  { label:'context',  bg:'color-mix(in srgb, var(--purple) 10%, transparent)', border:'color-mix(in srgb, var(--purple) 30%, transparent)', color:'var(--purple)' },
  done:     { label:'done',     bg:'var(--green-dim)',  border:'color-mix(in srgb, var(--green) 40%, transparent)',  color:'var(--green)' },
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
