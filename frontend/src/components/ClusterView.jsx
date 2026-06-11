import { useEffect, useRef } from 'react'

const CLUSTER_COLORS = [
  '#1D9E75','#378ADD','#BA7517','#7F77DD','#D85A30','#D4537E','#639922','#888780'
]
const CLUSTER_NAMES = ['Core','Utility','Data','Model','Config','API','Test','Other']

export default function ClusterView({ modules = [], fcgEdges = [], coreComponents = [] }) {
  const canvasRef = useRef()

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    const W = canvas.offsetWidth
    const H = canvas.offsetHeight
    canvas.width  = W * dpr
    canvas.height = H * dpr
    const ctx = canvas.getContext('2d')
    ctx.scale(dpr, dpr)
    drawCluster(ctx, W, H, modules, fcgEdges, coreComponents)
  }, [modules, fcgEdges, coreComponents])

  return (
    <div>
      <div style={{ background:'var(--bg2)', border:'1px solid var(--border)', borderRadius:'var(--radius-lg)', overflow:'hidden', marginBottom:12 }}>
        <canvas ref={canvasRef} style={{ width:'100%', height:380, display:'block' }} />
      </div>
      <div style={{ display:'flex', flexWrap:'wrap', gap:10, fontSize:12, color:'var(--txt2)' }}>
        {CLUSTER_COLORS.slice(0,5).map((c,i) => (
          <div key={i} style={{ display:'flex', alignItems:'center', gap:6 }}>
            <div style={{ width:10,height:10,borderRadius:'50%',background:c }} />
            {CLUSTER_NAMES[i]}
          </div>
        ))}
        <div style={{ display:'flex', alignItems:'center', gap:6 }}>
          <div style={{ width:10,height:10,borderRadius:'50%',background:'rgba(255,255,255,0.3)' }} />
          Node size = importance score
        </div>
      </div>
    </div>
  )
}

function drawCluster(ctx, W, H, modules, fcgEdges, coreComponents) {
  ctx.clearRect(0, 0, W, H)

  const mods = modules.length > 0 ? modules : [
    { name:'main', score:9 },{ name:'model', score:8.5 },{ name:'utils', score:7 },
  ]

  // Assign each module a cluster group
  const nodes = mods.map((m, i) => {
    const isCore = coreComponents.some(c => c.toLowerCase().includes(m.name.toLowerCase()))
    const clusterIdx = isCore ? 0 : i % CLUSTER_COLORS.length
    // Place in cluster groups
    const groupAngle = (clusterIdx / CLUSTER_COLORS.length) * Math.PI * 2
    const groupR = Math.min(W, H) * 0.28
    const jitter = (Math.random() - 0.5) * 70
    const score = typeof m.score === 'number' ? m.score : 7
    return {
      name: m.name,
      x: W / 2 + Math.cos(groupAngle) * groupR + jitter,
      y: H / 2 + Math.sin(groupAngle) * groupR + jitter,
      r: 5 + score * 1.4,
      color: CLUSTER_COLORS[clusterIdx],
      isCore,
      score,
    }
  })

  // Central hub
  const hub = { name:'hub', x: W/2, y: H/2, r: 20, color:'#5d8eff', isHub: true }

  // Draw hub spokes
  nodes.forEach(n => {
    ctx.save()
    ctx.beginPath()
    ctx.moveTo(hub.x, hub.y)
    ctx.lineTo(n.x, n.y)
    ctx.strokeStyle = 'rgba(255,255,255,0.04)'
    ctx.lineWidth = 0.8
    ctx.stroke()
    ctx.restore()
  })

  // Draw FCG edges between nodes
  fcgEdges.slice(0, 25).forEach(e => {
    const src = nodes.find(n => n.name === e.from || n.name.includes(e.from))
    const dst = nodes.find(n => n.name === e.to || n.name.includes(e.to))
    if (!src || !dst || src === dst) return
    ctx.save()
    ctx.beginPath()
    ctx.moveTo(src.x, src.y)
    ctx.lineTo(dst.x, dst.y)
    ctx.strokeStyle = 'rgba(255,255,255,0.06)'
    ctx.lineWidth = Math.min((e.weight || 1) * 0.4, 2)
    ctx.stroke()
    ctx.restore()
  })

  // Draw hub
  ctx.save()
  ctx.beginPath()
  ctx.arc(hub.x, hub.y, hub.r, 0, Math.PI * 2)
  ctx.fillStyle = '#5d8eff'
  ctx.globalAlpha = 0.9
  ctx.fill()
  ctx.globalAlpha = 1
  ctx.font = 'bold 9px Inter, sans-serif'
  ctx.fillStyle = 'white'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText('main', hub.x, hub.y)
  ctx.textBaseline = 'alphabetic'
  ctx.restore()

  // Draw module nodes
  nodes.forEach(n => {
    // Glow for core
    if (n.isCore) {
      ctx.save()
      ctx.beginPath()
      ctx.arc(n.x, n.y, n.r + 5, 0, Math.PI * 2)
      ctx.fillStyle = n.color + '22'
      ctx.fill()
      ctx.restore()
    }

    ctx.save()
    ctx.beginPath()
    ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2)
    ctx.fillStyle = n.color + (n.isCore ? 'dd' : '88')
    ctx.fill()
    ctx.restore()

    // Label
    ctx.save()
    ctx.font = `${Math.min(n.r * 0.55 + 6, 11)}px Inter, sans-serif`
    ctx.fillStyle = 'rgba(220,220,240,0.75)'
    ctx.textAlign = 'center'
    const label = n.name.length > 12 ? n.name.slice(0, 11) + '…' : n.name
    ctx.fillText(label, n.x, n.y + n.r + 13)
    ctx.restore()
  })
}
