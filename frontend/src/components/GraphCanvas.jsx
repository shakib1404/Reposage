/**
 * GraphCanvas
 *
 * Small-card (fullscreen=false): circle layout, 14-node cap, no interaction
 * Fullscreen (fullscreen=true):  list of ALL nodes on left, click → ego graph on right
 */
import { useEffect, useRef, useCallback, useState, useMemo } from 'react'
import { getTheme, useTheme, ink } from '../lib/theme'

// Canvas can't read CSS variables directly, so ink colours are resolved per
// draw. Dark values are the original literals; light uses the theme's ink.
const isLight = () => getTheme() === 'light'

// ── Palettes ──────────────────────────────────────────────────────────────────
const PALETTES = {
  green: ['#34d399', '#10b981', '#6ee7b7', '#059669', '#a7f3d0', '#065f46'],
  blue:  ['#60a5fa', '#3b82f6', '#93c5fd', '#2563eb', '#bfdbfe', '#1e40af'],
  multi: ['#f87171', '#fb923c', '#fbbf24', '#34d399', '#60a5fa', '#a78bfa',
          '#f472b6', '#4ade80', '#38bdf8', '#e879f9', '#facc15', '#2dd4bf'],
}

const MOD_COLORS = [
  '#34d399','#60a5fa','#f87171','#a78bfa','#fbbf24',
  '#f472b6','#38bdf8','#4ade80','#fb923c','#e879f9',
  '#2dd4bf','#facc15','#818cf8','#f43f5e','#06b6d4',
  '#84cc16','#c084fc','#fb7185','#22d3ee','#a3e635',
]

// ── Force layout (small-card only) ────────────────────────────────────────────
function computeForceLayout(names, edges, W, H) {
  if (!names.length) return {}
  const pos = {}
  names.forEach((n, i) => {
    const angle = (i / names.length) * Math.PI * 2 - Math.PI / 2
    const r = Math.min(W, H) * 0.36
    pos[n] = { x: W / 2 + Math.cos(angle) * r, y: H / 2 + Math.sin(angle) * r, vx: 0, vy: 0 }
  })
  const IDEAL = Math.max(70, Math.min(220, 700 / Math.sqrt(names.length)))
  const REPEL = 3500, SPRING = 0.06, DAMP = 0.82, ITERS = 250
  for (let iter = 0; iter < ITERS; iter++) {
    const cool = 1 - iter / ITERS
    for (let i = 0; i < names.length; i++)
      for (let j = i + 1; j < names.length; j++) {
        const a = pos[names[i]], b = pos[names[j]]
        const dx = a.x - b.x, dy = a.y - b.y
        const d = Math.sqrt(dx*dx + dy*dy) || 1
        const f = (REPEL/(d*d)) * cool
        a.vx += (dx/d)*f; a.vy += (dy/d)*f
        b.vx -= (dx/d)*f; b.vy -= (dy/d)*f
      }
    edges.forEach(e => {
      const a = pos[e.from], b = pos[e.to]; if (!a||!b) return
      const dx = b.x-a.x, dy = b.y-a.y, d = Math.sqrt(dx*dx+dy*dy)||1
      const f = (d-IDEAL)*SPRING*(e.weight||1)*cool
      a.vx+=(dx/d)*f; a.vy+=(dy/d)*f; b.vx-=(dx/d)*f; b.vy-=(dy/d)*f
    })
    names.forEach(n => {
      pos[n].vx += (W/2 - pos[n].x)*0.008*cool
      pos[n].vy += (H/2 - pos[n].y)*0.008*cool
      pos[n].x = Math.max(55, Math.min(W-55, pos[n].x + pos[n].vx))
      pos[n].y = Math.max(45, Math.min(H-45, pos[n].y + pos[n].vy))
      pos[n].vx *= DAMP; pos[n].vy *= DAMP
    })
  }
  return pos
}

// ── Small-card draw ───────────────────────────────────────────────────────────
function drawGraph(ctx, W, H, edges, names, positions, palette, hoveredNode, transform) {
  const { scale=1, tx=0, ty=0 } = transform || {}
  ctx.clearRect(0,0,W,H); ctx.save(); ctx.translate(tx,ty); ctx.scale(scale,scale)
  const inDeg = {}
  edges.forEach(e => { inDeg[e.to] = (inDeg[e.to]||0) + (e.weight||1) })
  edges.forEach(e => {
    const a = positions[e.from], b = positions[e.to]
    if (!a||!b||e.from===e.to) return
    const w = Math.min(0.6+(e.weight||1)*0.35, 3.5)
    const hl = hoveredNode && (e.from===hoveredNode||e.to===hoveredNode)
    ctx.save()
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y)
    ctx.strokeStyle = hl ? ink(0.55) : ink(isLight() ? 0.16 : 0.1)
    ctx.lineWidth = hl ? w*1.8 : w; ctx.stroke()
    const ang = Math.atan2(b.y-a.y,b.x-a.x), nr=10
    const bx=b.x-nr*Math.cos(ang), by=b.y-nr*Math.sin(ang)
    ctx.beginPath()
    ctx.moveTo(bx-6*Math.cos(ang-0.45),by-6*Math.sin(ang-0.45))
    ctx.lineTo(bx,by)
    ctx.lineTo(bx-6*Math.cos(ang+0.45),by-6*Math.sin(ang+0.45))
    ctx.strokeStyle = hl ? ink(0.6) : ink(isLight() ? 0.3 : 0.2)
    ctx.lineWidth = hl?1.5:1; ctx.stroke(); ctx.restore()
  })
  names.forEach((n,i) => {
    const p = positions[n]; if (!p) return
    const r = 5+Math.min((inDeg[n]||0)*1.1,9), hl = n===hoveredNode
    const clr = palette[i%palette.length]
    if (hl) { ctx.save(); ctx.beginPath(); ctx.arc(p.x,p.y,r+7,0,Math.PI*2); ctx.strokeStyle=clr+'aa'; ctx.lineWidth=2; ctx.stroke(); ctx.restore() }
    ctx.save(); ctx.beginPath(); ctx.arc(p.x,p.y,r,0,Math.PI*2)
    ctx.fillStyle=clr+(hl?'ff':'cc'); ctx.shadowColor=clr; ctx.shadowBlur=hl?12:4; ctx.fill(); ctx.restore()
    ctx.save()
    ctx.font=`${Math.max(9,Math.min(12,10+(scale-1)*3))}px 'JetBrains Mono',monospace`
    ctx.fillStyle = hl ? ink(1) : (isLight() ? ink(0.72) : 'rgba(210,210,235,0.8)'); ctx.textAlign='center'
    const maxLen=W>400?18:11, label=n.length>maxLen?n.slice(0,maxLen-1)+'…':n
    ctx.fillText(label,p.x,p.y+r+13); ctx.restore()
  })
  ctx.restore()
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Ego-graph canvas  (right panel in fullscreen)
// ═══════════════════════════════════════════════════════════════════════════════
function EgoCanvas({ selected, egoEdges, egoNodes, nodeModule, moduleColor, onNodeClick }) {
  const canvasRef    = useRef()
  const transformRef = useRef({ scale:1, tx:0, ty:0 })
  const dragRef      = useRef(null)
  const hoveredRef   = useRef(null)
  const wasDrag      = useRef(false)
  const theme        = useTheme()

  // Radial layout: selected at center, neighbors around it
  const layout = useMemo(() => {
    const neighbors = egoNodes.filter(n => n !== selected)
    const cnt = neighbors.length
    const LW = 700, LH = 540
    const cx = LW/2, cy = LH/2
    const r  = Math.min(210, Math.max(90, cnt * 30))
    const pos = {}
    pos[selected] = { x: cx, y: cy }
    neighbors.forEach((n, i) => {
      const angle = (i / cnt) * Math.PI * 2 - Math.PI / 2
      pos[n] = { x: cx + Math.cos(angle)*r, y: cy + Math.sin(angle)*r }
    })
    return pos
  }, [selected, egoNodes])

  const fitTr = useCallback((W, H) => {
    const pts = Object.values(layout)
    if (!pts.length) return { scale:1, tx:0, ty:0 }
    const xs=pts.map(p=>p.x), ys=pts.map(p=>p.y)
    const minX=Math.min(...xs)-90, maxX=Math.max(...xs)+90
    const minY=Math.min(...ys)-90, maxY=Math.max(...ys)+90
    const scale=Math.min(W/(maxX-minX), H/(maxY-minY), 1.4)
    return { scale, tx: W/2-((minX+maxX)/2)*scale, ty: H/2-((minY+maxY)/2)*scale }
  }, [layout])

  const draw = useCallback(() => {
    const canvas = canvasRef.current; if (!canvas) return
    const dpr = window.devicePixelRatio||1
    const ctx  = canvas.getContext('2d')
    const W    = canvas.width/dpr, H = canvas.height/dpr
    const { scale, tx, ty } = transformRef.current

    ctx.setTransform(dpr,0,0,dpr,0,0)
    ctx.clearRect(0,0,W,H)
    ctx.save(); ctx.translate(tx,ty); ctx.scale(scale,scale)

    const inDeg={}, outDeg={}
    egoEdges.forEach(e => { outDeg[e.from]=(outDeg[e.from]||0)+1; inDeg[e.to]=(inDeg[e.to]||0)+1 })

    // ── Edges ──────────────────────────────────────────────────────────────
    egoEdges.forEach(e => {
      const a=layout[e.from], b=layout[e.to]; if (!a||!b) return
      const isOut  = e.from === selected
      const isIn   = e.to   === selected
      const hl     = hoveredRef.current && (e.from===hoveredRef.current||e.to===hoveredRef.current)
      const color  = isOut ? (isLight() ? '#10a37a' : '#34d399') : isIn ? (isLight() ? '#3b82f6' : '#60a5fa') : (isLight() ? ink(0.25) : 'rgba(200,200,220,0.25)')
      const w      = Math.min(0.9+(e.weight||1)*0.35, 3)

      ctx.save()
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y)
      ctx.strokeStyle = hl ? color : color
      ctx.globalAlpha = hl ? 1 : (isOut||isIn ? 0.75 : 0.35)
      ctx.lineWidth   = hl ? w*2 : w; ctx.stroke()

      // Arrow
      const ang=Math.atan2(b.y-a.y, b.x-a.x)
      const nr = (b===layout[selected]) ? 22 : 13
      const bx=b.x-nr*Math.cos(ang), by=b.y-nr*Math.sin(ang)
      ctx.beginPath()
      ctx.moveTo(bx-7*Math.cos(ang-0.4),by-7*Math.sin(ang-0.4))
      ctx.lineTo(bx,by)
      ctx.lineTo(bx-7*Math.cos(ang+0.4),by-7*Math.sin(ang+0.4))
      ctx.lineWidth = hl?2:1.2; ctx.stroke()

      // Weight badge
      if ((e.weight||1) > 1) {
        ctx.globalAlpha=0.55; ctx.font='9px monospace'; ctx.fillStyle=color
        ctx.textAlign='center'; ctx.textBaseline='middle'
        ctx.fillText(`×${e.weight}`, (a.x+b.x)/2+5, (a.y+b.y)/2-5)
      }
      ctx.restore()
    })

    // ── Nodes ──────────────────────────────────────────────────────────────
    egoNodes.forEach(n => {
      const p=layout[n]; if (!p) return
      const isSel  = n === selected
      const mod    = nodeModule[n] || ''
      const color  = moduleColor[mod] || '#60a5fa'
      const r      = isSel ? 20 : 9 + Math.min((inDeg[n]||0)*0.7, 6)
      const hl     = n === hoveredRef.current

      // Glow ring
      if (hl||isSel) {
        ctx.save(); ctx.beginPath(); ctx.arc(p.x,p.y,r+(isSel?9:5),0,Math.PI*2)
        ctx.strokeStyle=color+'88'; ctx.lineWidth=2; ctx.stroke(); ctx.restore()
      }

      ctx.save(); ctx.beginPath(); ctx.arc(p.x,p.y,r,0,Math.PI*2)
      if (isSel) {
        ctx.fillStyle=isLight() ? '#ffffff' : '#0f172a'; ctx.strokeStyle=color
        ctx.lineWidth=3; ctx.shadowColor=color; ctx.shadowBlur=22
        ctx.fill(); ctx.stroke()
      } else {
        ctx.fillStyle=color+(hl?'ff':'cc')
        ctx.shadowColor=color; ctx.shadowBlur=hl?14:5; ctx.fill()
      }
      ctx.restore()

      // Label
      ctx.save()
      ctx.font = isSel
        ? `bold 12px 'JetBrains Mono',monospace`
        : `10px 'JetBrains Mono',monospace`
      ctx.fillStyle = isSel ? color : (hl ? ink(1) : (isLight() ? ink(0.75) : 'rgba(210,215,240,0.85)'))
      ctx.textAlign = 'center'
      if (isSel) {
        ctx.textBaseline='middle'; ctx.fillText(n,p.x,p.y)
      } else {
        ctx.textBaseline='top'
        const label=n.length>20?n.slice(0,19)+'…':n
        ctx.fillText(label,p.x,p.y+r+4)
      }
      ctx.restore()

      // Direction arrow badge (→ ← ↔)
      if (!isSel) {
        const goesOut = egoEdges.some(e=>e.from===selected&&e.to===n)
        const comesIn = egoEdges.some(e=>e.to===selected&&e.from===n)
        const badge   = goesOut&&comesIn ? '↔' : goesOut ? '→' : '←'
        const bclr    = isLight()
          ? (goesOut&&comesIn ? '#b7791f' : goesOut ? '#10a37a' : '#3b82f6')
          : (goesOut&&comesIn ? '#fbbf24' : goesOut ? '#34d399' : '#60a5fa')
        ctx.save()
        ctx.font='9px sans-serif'; ctx.fillStyle=bclr
        ctx.textAlign='left'; ctx.textBaseline='top'
        ctx.fillText(badge, p.x+r+3, p.y-r-1)
        ctx.restore()
      }
    })

    ctx.restore()
  }, [selected, egoEdges, egoNodes, layout, nodeModule, moduleColor, theme]) // eslint-disable-line react-hooks/exhaustive-deps

  // Initial setup whenever layout changes
  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return
    const dpr = window.devicePixelRatio||1
    const W   = canvas.offsetWidth  || 700
    const H   = canvas.offsetHeight || 540
    canvas.width=W*dpr; canvas.height=H*dpr
    transformRef.current = fitTr(W, H)
    draw()
  }, [layout, draw, fitTr])

  const onWheel = useCallback(e => {
    e.preventDefault()
    const canvas=canvasRef.current, rect=canvas.getBoundingClientRect()
    const mx=e.clientX-rect.left, my=e.clientY-rect.top
    const factor=e.deltaY<0?1.12:0.89
    const {scale,tx,ty}=transformRef.current
    const ns=Math.max(0.15,Math.min(8,scale*factor))
    transformRef.current={scale:ns,tx:mx-(mx-tx)*(ns/scale),ty:my-(my-ty)*(ns/scale)}
    draw()
  }, [draw])

  const onMouseDown = useCallback(e => {
    wasDrag.current=false
    dragRef.current={sx:e.clientX,sy:e.clientY,tx:transformRef.current.tx,ty:transformRef.current.ty}
  }, [])

  const onMouseMove = useCallback(e => {
    const canvas=canvasRef.current; if (!canvas) return
    if (dragRef.current) {
      const dx=e.clientX-dragRef.current.sx, dy=e.clientY-dragRef.current.sy
      if (Math.abs(dx)>3||Math.abs(dy)>3) wasDrag.current=true
      transformRef.current={...transformRef.current,tx:dragRef.current.tx+dx,ty:dragRef.current.ty+dy}
      draw(); return
    }
    const rect=canvas.getBoundingClientRect()
    const {scale,tx,ty}=transformRef.current
    const cx=(e.clientX-rect.left-tx)/scale, cy=(e.clientY-rect.top-ty)/scale
    let hit=null
    for (const [n,p] of Object.entries(layout)) {
      const r=(n===selected?24:16)
      if ((cx-p.x)**2+(cy-p.y)**2<r*r){hit=n;break}
    }
    if (hit!==hoveredRef.current) {
      hoveredRef.current=hit
      canvas.style.cursor=hit&&hit!==selected?'pointer':'grab'
      draw()
    }
  }, [layout, selected, draw])

  const onMouseUp = useCallback(() => { dragRef.current=null }, [])

  const onClick = useCallback(e => {
    if (wasDrag.current) return
    const canvas=canvasRef.current; if (!canvas) return
    const rect=canvas.getBoundingClientRect()
    const {scale,tx,ty}=transformRef.current
    const cx=(e.clientX-rect.left-tx)/scale, cy=(e.clientY-rect.top-ty)/scale
    for (const [n,p] of Object.entries(layout)) {
      if (n===selected) continue
      if ((cx-p.x)**2+(cy-p.y)**2<16*16){onNodeClick(n);break}
    }
  }, [layout, selected, onNodeClick])

  const onDblClick = useCallback(() => {
    const canvas=canvasRef.current; if (!canvas) return
    const dpr=window.devicePixelRatio||1
    transformRef.current=fitTr(canvas.width/dpr,canvas.height/dpr)
    draw()
  }, [draw, fitTr])

  return (
    <canvas
      ref={canvasRef}
      onWheel={onWheel} onMouseDown={onMouseDown} onMouseMove={onMouseMove}
      onMouseUp={onMouseUp} onMouseLeave={onMouseUp} onClick={onClick} onDoubleClick={onDblClick}
      style={{ width:'100%', height:'100%', display:'block', cursor:'grab', userSelect:'none' }}
    />
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Fullscreen: list panel + ego graph
// ═══════════════════════════════════════════════════════════════════════════════
function FullscreenNodeGraph({ edges, nodes, colorScheme }) {
  const [selected, setSelected] = useState(null)
  const [search,   setSearch]   = useState('')

  // All unique nodes
  const allNames = useMemo(() =>
    [...new Set([...edges.map(e=>e.from), ...edges.map(e=>e.to), ...nodes])],
    [edges, nodes])

  // node → module mapping
  const nodeModule = useMemo(() => {
    const m={}
    edges.forEach(e => {
      if (e.from) m[e.from] = e.from_module || (e.from_mod_key||'').split('.').pop() || ''
      if (e.to)   m[e.to]   = e.to_module   || (e.to_mod_key  ||'').split('.').pop() || ''
    })
    return m
  }, [edges])

  const hasModules = useMemo(() => Object.values(nodeModule).some(v=>v), [nodeModule])

  // module → color
  const moduleColor = useMemo(() => {
    const mods=[...new Set(Object.values(nodeModule))].filter(Boolean)
    const mc={}
    mods.forEach((m,i)=>{ mc[m]=MOD_COLORS[i%MOD_COLORS.length] })
    return mc
  }, [nodeModule])

  // Group by module
  const groups = useMemo(() => {
    const g={}
    allNames.forEach(n => {
      const mod=nodeModule[n]||'(no module)'
      ;(g[mod]=g[mod]||[]).push(n)
    })
    return g
  }, [allNames, nodeModule])

  // Connection count per node
  const connCount = useMemo(() => {
    const cnt={}
    edges.forEach(e=>{cnt[e.from]=(cnt[e.from]||0)+1;cnt[e.to]=(cnt[e.to]||0)+1})
    return cnt
  }, [edges])

  // Select first node on mount
  useEffect(() => {
    if (!selected && allNames.length>0) setSelected(allNames[0])
  }, [allNames]) // eslint-disable-line

  // Ego edges/nodes for selected
  const egoEdges = useMemo(() =>
    selected ? edges.filter(e=>e.from===selected||e.to===selected) : [],
    [selected, edges])

  const egoNodes = useMemo(() =>
    selected ? [...new Set([selected,...egoEdges.map(e=>e.from),...egoEdges.map(e=>e.to)])] : [],
    [selected, egoEdges])

  // Filtered list
  const filteredGroups = useMemo(() => {
    if (!search) return groups
    const q=search.toLowerCase()
    const fg={}
    Object.entries(groups).forEach(([mod,names])=>{
      const filtered=names.filter(n=>n.toLowerCase().includes(q)||mod.toLowerCase().includes(q))
      if (filtered.length) fg[mod]=filtered
    })
    return fg
  }, [groups, search])

  const outCount = selected ? egoEdges.filter(e=>e.from===selected).length : 0
  const inCount  = selected ? egoEdges.filter(e=>e.to===selected).length   : 0

  return (
    <div style={{ display:'flex', height:'100%', overflow:'hidden' }}>

      {/* ── Left: node list ──────────────────────────────────────────────── */}
      <div style={{
        width:240, flexShrink:0,
        borderRight:'1px solid var(--border)',
        display:'flex', flexDirection:'column', overflow:'hidden',
        background:'var(--bg)',
      }}>
        {/* Search + stats */}
        <div style={{ padding:'10px 10px 6px', borderBottom:'1px solid var(--border)', flexShrink:0 }}>
          <input
            value={search}
            onChange={e=>setSearch(e.target.value)}
            placeholder="Search nodes…"
            style={{
              width:'100%', padding:'5px 8px', boxSizing:'border-box',
              background:'var(--bg3)', border:'1px solid var(--border2)',
              borderRadius:'var(--radius)', color:'var(--txt)',
              fontSize:12, fontFamily:'var(--mono)', outline:'none',
            }}
          />
          <div style={{ fontSize:10, color:'var(--txt3)', marginTop:5, display:'flex', gap:10 }}>
            <span>{allNames.length} nodes</span>
            <span>{edges.length} edges</span>
          </div>
        </div>

        {/* List */}
        <div style={{ overflowY:'auto', flex:1 }}>
          {Object.entries(filteredGroups).map(([mod, names]) => (
            <div key={mod}>
              {hasModules && (
                <div style={{
                  padding:'7px 10px 4px',
                  fontSize:10, fontWeight:700, letterSpacing:'0.07em',
                  textTransform:'uppercase',
                  color: moduleColor[mod] || 'var(--txt3)',
                  borderBottom:`1px solid ${moduleColor[mod]||'var(--border)'}30`,
                  position:'sticky', top:0, background:'var(--bg)',
                }}>
                  {mod} <span style={{ fontWeight:400, opacity:0.6 }}>({names.length})</span>
                </div>
              )}
              {names.map(n => {
                const isSel = n === selected
                const cnt   = connCount[n] || 0
                const mod2  = nodeModule[n] || ''
                const clr   = moduleColor[mod2] || 'var(--txt2)'
                return (
                  <div
                    key={n}
                    onClick={() => setSelected(n)}
                    style={{
                      padding:'4px 10px 4px 18px',
                      cursor:'pointer',
                      background: isSel ? 'var(--accent-dim)' : 'transparent',
                      borderLeft: `2px solid ${isSel ? 'var(--accent)' : 'transparent'}`,
                      display:'flex', alignItems:'center', justifyContent:'space-between', gap:6,
                      transition:'background 0.1s',
                    }}
                    onMouseOver={e=>{ if(!isSel) e.currentTarget.style.background='var(--bg3)' }}
                    onMouseOut={e =>{ if(!isSel) e.currentTarget.style.background='transparent' }}
                  >
                    <span style={{
                      fontSize:12, fontFamily:'var(--mono)',
                      color: isSel ? 'var(--accent)' : clr,
                      overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', flex:1,
                    }}>
                      {n}
                    </span>
                    {cnt>0 && (
                      <span style={{
                        fontSize:10, color:'var(--txt3)', flexShrink:0,
                        background:'var(--bg3)', borderRadius:8, padding:'1px 5px',
                      }}>
                        {cnt}
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      </div>

      {/* ── Right: ego graph ─────────────────────────────────────────────── */}
      <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden', background:'var(--graph-bg)' }}>

        {/* Header bar */}
        <div style={{
          padding:'8px 14px', borderBottom:'1px solid var(--border)',
          display:'flex', alignItems:'center', gap:10,
          background:'var(--graph-bar)', flexShrink:0,
        }}>
          {selected ? (
            <>
              <span style={{ fontFamily:'var(--mono)', fontWeight:700, fontSize:13, color:'var(--accent)' }}>
                {selected}
              </span>
              {nodeModule[selected] && (
                <span style={{ fontSize:11, color:'var(--txt3)' }}>in {nodeModule[selected]}</span>
              )}
              <div style={{ marginLeft:'auto', display:'flex', gap:14, fontSize:11, color:'var(--txt3)' }}>
                <span style={{ color:'var(--green)' }}>→ {outCount} out</span>
                <span style={{ color:'var(--blue)' }}>← {inCount} in</span>
                <span style={{ opacity:0.5 }}>scroll=zoom · drag=pan · dbl-click=fit · click node=navigate</span>
              </div>
            </>
          ) : (
            <span style={{ fontSize:12, color:'var(--txt3)' }}>Select a node from the list</span>
          )}
        </div>

        {/* Canvas area */}
        <div style={{ flex:1, position:'relative', overflow:'hidden' }}>
          {selected && egoNodes.length > 0 ? (
            <EgoCanvas
              key={selected}
              selected={selected}
              egoEdges={egoEdges}
              egoNodes={egoNodes}
              nodeModule={nodeModule}
              moduleColor={moduleColor}
              onNodeClick={setSelected}
            />
          ) : (
            <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:'100%', color:'var(--txt3)', fontSize:13 }}>
              {selected ? 'This node has no connections.' : 'Select a node from the list.'}
            </div>
          )}

          {/* Legend */}
          {selected && (
            <div style={{
              position:'absolute', bottom:14, right:14,
              display:'flex', gap:14, fontSize:11,
              background:'var(--graph-bar)', padding:'5px 12px', borderRadius:6,
              border:'1px solid var(--border)',
            }}>
              <span style={{ color:'var(--green)' }}>→ outgoing</span>
              <span style={{ color:'var(--blue)' }}>← incoming</span>
              <span style={{ color:'var(--yellow)' }}>↔ both</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Compact export (GraphCanvasCompact) — unchanged
// ═══════════════════════════════════════════════════════════════════════════════
export function GraphCanvasCompact({ edges=[], nodes=[], height=220, colorScheme='green' }) {
  const canvasRef=useRef()
  const theme=useTheme()
  useEffect(()=>{
    const canvas=canvasRef.current; if(!canvas) return
    const dpr=window.devicePixelRatio||1
    const W=canvas.offsetWidth, H=canvas.offsetHeight
    canvas.width=W*dpr; canvas.height=H*dpr
    const ctx=canvas.getContext('2d'); ctx.scale(dpr,dpr)
    const palette=PALETTES[colorScheme]||PALETTES.green
    const names=[...new Set([...edges.map(e=>e.from),...edges.map(e=>e.to),...nodes])].slice(0,14)
    if(!names.length) return
    const positions={}
    names.forEach((n,i)=>{
      const angle=(i/names.length)*Math.PI*2-Math.PI/2
      const r=Math.min(W,H)*0.36
      positions[n]={x:W/2+Math.cos(angle)*r, y:H/2+Math.sin(angle)*r}
    })
    drawGraph(ctx,W,H,edges,names,positions,palette,null,{})
  },[edges,nodes,colorScheme,theme])
  return <canvas ref={canvasRef} style={{width:'100%',height,display:'block',borderRadius:6}}/>
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Compact canvas (small-card, circle layout, 14-node cap)
// ═══════════════════════════════════════════════════════════════════════════════
function CompactCanvas({ edges, nodes, height, colorScheme }) {
  const canvasRef    = useRef()
  const layoutRef    = useRef(null)
  const namesRef     = useRef([])
  const transformRef = useRef({ scale:1, tx:0, ty:0 })
  const hoveredRef   = useRef(null)
  const palette      = PALETTES[colorScheme||'green']
  const theme        = useTheme()

  const redraw = useCallback(() => {
    const canvas = canvasRef.current; if (!canvas||!layoutRef.current) return
    const dpr = window.devicePixelRatio||1
    const ctx  = canvas.getContext('2d')
    ctx.save(); ctx.scale(dpr, dpr)
    drawGraph(ctx, canvas.width/dpr, canvas.height/dpr,
      edges, namesRef.current, layoutRef.current, palette, hoveredRef.current, transformRef.current)
    ctx.restore()
  }, [edges, palette, theme]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return
    const dpr = window.devicePixelRatio||1
    const W = canvas.offsetWidth||400, H = canvas.offsetHeight||height
    canvas.width = W*dpr; canvas.height = H*dpr
    const names = [...new Set([...edges.map(e=>e.from), ...edges.map(e=>e.to), ...nodes])].slice(0, 14)
    namesRef.current = names
    const pos = {}
    names.forEach((n, i) => {
      const angle = (i/names.length)*Math.PI*2 - Math.PI/2
      pos[n] = { x: W/2+Math.cos(angle)*Math.min(W,H)*0.36, y: H/2+Math.sin(angle)*Math.min(W,H)*0.36 }
    })
    layoutRef.current = pos
    transformRef.current = { scale:1, tx:0, ty:0 }
    redraw()
  }, [edges, nodes, height, redraw])

  const onMouseMove = useCallback(e => {
    const canvas = canvasRef.current; if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const { scale, tx, ty } = transformRef.current
    const cx = (e.clientX-rect.left-tx)/scale, cy = (e.clientY-rect.top-ty)/scale
    const inDeg = {}; edges.forEach(edge => { inDeg[edge.to] = (inDeg[edge.to]||0)+(edge.weight||1) })
    let hit = null
    for (const n of namesRef.current) {
      const p = layoutRef.current?.[n]; if (!p) continue
      const r = 5+Math.min((inDeg[n]||0)*1.1, 9)+4
      if ((cx-p.x)**2+(cy-p.y)**2 < r*r) { hit=n; break }
    }
    if (hit !== hoveredRef.current) { hoveredRef.current=hit; redraw() }
  }, [edges, redraw])

  return (
    <canvas
      ref={canvasRef}
      onMouseMove={onMouseMove}
      style={{ width:'100%', height, display:'block', borderRadius:6, userSelect:'none' }}
    />
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
//  Default export — routes to FullscreenNodeGraph or small-card
// ═══════════════════════════════════════════════════════════════════════════════
export default function GraphCanvas({ edges=[], nodes=[], height=220, colorScheme='green', fullscreen=false }) {
  if (fullscreen) return <FullscreenNodeGraph edges={edges} nodes={nodes} colorScheme={colorScheme} />
  return <CompactCanvas edges={edges} nodes={nodes} height={height} colorScheme={colorScheme} />
}
