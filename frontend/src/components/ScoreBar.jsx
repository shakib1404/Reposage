const COLORS = ['#378ADD','#1D9E75','#BA7517','#D85A30','#7F77DD','#639922','#D4537E','#888780']

export default function ScoreBar({ name, score, index = 0 }) {
  const pct = Math.min((score / 10) * 100, 100)
  const color = COLORS[index % COLORS.length]

  return (
    <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:6 }}>
      <div style={{ width:100, fontSize:12, color:'var(--txt2)', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', flexShrink:0 }} title={name}>
        {name}
      </div>
      <div style={{ flex:1, height:5, background:'var(--bg3)', borderRadius:3, overflow:'hidden' }}>
        <div style={{ height:'100%', width: pct + '%', background: color, borderRadius:3, transition:'width 0.6s ease' }} />
      </div>
      <div style={{ width:28, fontSize:11, color:'var(--txt2)', textAlign:'right', flexShrink:0 }}>
        {score.toFixed(1)}
      </div>
    </div>
  )
}
