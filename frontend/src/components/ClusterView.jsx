/**
 * ClusterView — the module dependency graph grouped by its real communities.
 *
 * The previous version assigned a "cluster" with `index % 8` and labelled the
 * groups Core / Utility / Data / Model / Config …, none of which measured
 * anything: renaming a file moved it to a different group, and the legend
 * showed five names for eight colours. Clusters now come from the backend,
 * which runs greedy modularity maximisation over the import graph, so a group
 * here is a set of modules that genuinely depend on each other.
 */
import { useEffect, useRef, useMemo, useState } from 'react'

// Golden-angle hue stepping: distinct, evenly separated colours for however
// many clusters a repo turns out to have, instead of a fixed palette of 8
// that silently wrapped around on the 9th.
function clusterColor(i, total) {
  if (total <= 0) return 'hsl(215 80% 62%)'
  const hue = (i * 137.508) % 360
  return `hsl(${hue.toFixed(1)} 68% 60%)`
}
const ISOLATED_COLOR = 'hsl(220 8% 45%)'

export default function ClusterView({
  modules = [], fcgEdges = [], coreComponents = [], clusters = [], height = null,
}) {
  const canvasRef = useRef()
  const [hover, setHover] = useState(null)

  // Fall back to a single implicit cluster for analyses saved before the
  // backend started emitting them, so old history entries still render.
  const groups = useMemo(() => {
    if (clusters.length) return clusters
    if (!modules.length) return []
    return [{ id: 1, label: 'Cluster 1', anchor: '', isolated: false,
              size: modules.length, modules: modules.map(m => m.name) }]
  }, [clusters, modules])

  const layout = useMemo(
    () => buildLayout(groups, modules, coreComponents),
    [groups, modules, coreComponents],
  )

  // A fixed 380px fitted 3 clusters and cropped the ring at 8. Grow with the
  // count so every group stays inside the frame.
  const canvasH = height ?? Math.min(300 + groups.length * 34, 620)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    const W = canvas.offsetWidth
    const H = canvas.offsetHeight
    canvas.width = W * dpr
    canvas.height = H * dpr
    const ctx = canvas.getContext('2d')
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    draw(ctx, W, H, layout, fcgEdges, hover)
  }, [layout, fcgEdges, hover, canvasH])

  if (!groups.length) {
    return (
      <div style={{ color: 'var(--txt3)', fontSize: 12, padding: '14px 4px' }}>
        No modules to cluster.
      </div>
    )
  }

  const onMove = e => {
    const r = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - r.left, y = e.clientY - r.top
    const W = r.width, H = r.height
    const pos = placeAll(layout, W, H)
    let found = null
    for (const n of pos) {
      if ((n.x - x) ** 2 + (n.y - y) ** 2 <= (n.r + 3) ** 2) { found = n; break }
    }
    setHover(found ? { name: found.name, cluster: found.cluster, x, y } : null)
  }

  return (
    <div>
      <div style={{
        background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', overflow: 'hidden',
        marginBottom: 12, position: 'relative',
      }}>
        <canvas
          ref={canvasRef}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
          style={{ width: '100%', height: canvasH, display: 'block' }}
        />
        {hover && (
          <div style={{
            position: 'absolute', left: Math.min(hover.x + 10, 9999), top: hover.y + 10,
            background: 'var(--bg3)', border: '1px solid var(--border2)',
            borderRadius: 6, padding: '4px 9px', fontSize: 11,
            fontFamily: 'var(--mono)', color: 'var(--txt)', pointerEvents: 'none',
            whiteSpace: 'nowrap', zIndex: 5,
          }}>
            {hover.name}
            <span style={{ color: 'var(--txt3)' }}> · Cluster {hover.cluster}</span>
          </div>
        )}
      </div>

      {/* One legend row per cluster — all of them, not a fixed first five. */}
      {/* A grid, not a flex row: with flex the final entry on a line ran past
          the container and was cut mid-word ("Cluster 6 · 2 modules · se").
          auto-fit columns always give each entry a whole cell. */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
        gap: '6px 14px', fontSize: 11.5,
      }}>
        {groups.map((c, i) => (
          <div key={c.id} style={{
            display: 'flex', alignItems: 'center', gap: 6,
            minWidth: 0, overflow: 'hidden', whiteSpace: 'nowrap',
            textOverflow: 'ellipsis',
          }}>
            <span style={{
              width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
              background: c.isolated ? ISOLATED_COLOR : clusterColor(i, groups.length),
            }} />
            <span style={{ color: 'var(--txt)' }}>{c.label}</span>
            <span style={{ color: 'var(--txt3)' }}>
              {c.size} module{c.size === 1 ? '' : 's'}
              {c.anchor && !c.isolated ? ` · ${c.anchor}` : ''}
              {c.isolated ? ' · unconnected' : ''}
            </span>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11, color: 'var(--txt3)', marginTop: 8, lineHeight: 1.55 }}>
        Communities found by modularity maximisation over the import graph.
        Node size = module importance score; a ring marks a core component.
      </div>
    </div>
  )
}

// ── Layout ───────────────────────────────────────────────────────────────────
// Deterministic: the old version jittered with Math.random(), so every repaint
// moved every node and the picture never looked like the same repo twice.
function buildLayout(groups, modules, coreComponents) {
  const byName = new Map(modules.map(m => [m.name, m]))
  const core = new Set(coreComponents.map(c => String(c).toLowerCase()))
  return groups.map((g, gi) => ({
    ...g,
    color: g.isolated ? ISOLATED_COLOR : clusterColor(gi, groups.length),
    nodes: g.modules.map((name, ni) => {
      const m = byName.get(name) || {}
      const score = typeof m.score === 'number' ? m.score : 5
      return {
        name,
        short: m.short_name || name.split('.').pop(),
        cluster: g.id,
        score,
        r: 4 + Math.min(score, 10) * 1.1,
        isCore: core.has(String(name).toLowerCase()) ||
                core.has(String(m.short_name || '').toLowerCase()),
        ni,
      }
    }),
  }))
}

/** Positions for every node, given the canvas size. */
function placeAll(layout, W, H) {
  const out = []
  const n = layout.length
  const cx = W / 2, cy = H / 2
  // Push the ring out as the cluster count grows: at a fixed 0.3 the halos of
  // 8 clusters overlapped in the middle and the picture read as one blob.
  const ringR = n > 1
    ? Math.min(W, H) * Math.min(0.30 + n * 0.022, 0.42)
    : 0

  layout.forEach((g, gi) => {
    const a = (gi / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2
    const gx = cx + Math.cos(a) * ringR
    const gy = cy + Math.sin(a) * ringR
    // Members sit on a small spiral around their group's centre, so a big
    // cluster stays readable instead of collapsing into one blob.
    const k = g.nodes.length
    g.nodes.forEach((nd, i) => {
      if (k === 1) { out.push({ ...nd, x: gx, y: gy, color: g.color }); return }
      const turn = i * 2.39996                      // golden angle
      const rad  = 7 + Math.sqrt(i + 0.5) * (k > 24 ? 7.5 : 10)
      out.push({
        ...nd,
        x: gx + Math.cos(turn) * rad,
        y: gy + Math.sin(turn) * rad,
        color: g.color,
      })
    })
  })
  return out
}

function draw(ctx, W, H, layout, fcgEdges, hover) {
  ctx.clearRect(0, 0, W, H)
  const nodes = placeAll(layout, W, H)
  const pos = new Map(nodes.map(n => [n.name, n]))

  // Cluster halos, drawn first so nodes sit on top.
  layout.forEach(g => {
    const mem = nodes.filter(n => n.cluster === g.id)
    if (!mem.length) return
    const mx = mem.reduce((s, n) => s + n.x, 0) / mem.length
    const my = mem.reduce((s, n) => s + n.y, 0) / mem.length
    const rad = Math.max(...mem.map(n => Math.hypot(n.x - mx, n.y - my) + n.r)) + 10
    ctx.save()
    ctx.beginPath()
    ctx.arc(mx, my, rad, 0, Math.PI * 2)
    ctx.fillStyle = g.color.replace('hsl(', 'hsla(').replace(')', ' / 0.07)')
    ctx.fill()
    ctx.strokeStyle = g.color.replace('hsl(', 'hsla(').replace(')', ' / 0.28)')
    ctx.setLineDash([3, 3])
    ctx.lineWidth = 1
    ctx.stroke()
    ctx.setLineDash([])
    // Label the group on the halo itself, so a cluster is identifiable in the
    // picture and not only in the legend.
    ctx.font = '600 10px Inter, sans-serif'
    ctx.fillStyle = g.color
    ctx.textAlign = 'center'
    // Clamp: a cluster near the top edge had its label drawn at a negative y
    // and simply vanished — Cluster 1 was unlabelled in the picture.
    const ly = my - rad - 4
    ctx.fillText(g.label, Math.max(32, Math.min(mx, W - 32)),
                 ly < 10 ? Math.min(my + rad + 13, H - 3) : ly)
    ctx.restore()
  })

  // Call edges between modules we actually placed.
  ctx.save()
  ctx.strokeStyle = 'rgba(255,255,255,0.055)'
  fcgEdges.slice(0, 160).forEach(e => {
    const a = pos.get(e.from) || [...pos.values()].find(n => n.short === e.from)
    const b = pos.get(e.to)   || [...pos.values()].find(n => n.short === e.to)
    if (!a || !b || a === b) return
    ctx.beginPath()
    ctx.moveTo(a.x, a.y)
    ctx.lineTo(b.x, b.y)
    ctx.lineWidth = Math.min((e.weight || 1) * 0.35, 1.8)
    ctx.stroke()
  })
  ctx.restore()

  nodes.forEach(n => {
    const lit = hover && hover.name === n.name
    if (n.isCore) {
      ctx.save()
      ctx.beginPath()
      ctx.arc(n.x, n.y, n.r + 3.5, 0, Math.PI * 2)
      ctx.strokeStyle = n.color
      ctx.lineWidth = 1.2
      ctx.globalAlpha = 0.65
      ctx.stroke()
      ctx.restore()
    }
    ctx.save()
    ctx.beginPath()
    ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2)
    ctx.fillStyle = n.color
    ctx.globalAlpha = lit ? 1 : n.isCore ? 0.92 : 0.66
    ctx.fill()
    ctx.restore()
  })

  // Only label the biggest nodes — labelling 90 modules is unreadable.
  const labelled = [...nodes].sort((a, b) => b.score - a.score).slice(0, 14)
  ctx.save()
  ctx.font = '9.5px Inter, sans-serif'
  ctx.fillStyle = 'rgba(220,220,240,0.72)'
  ctx.textAlign = 'center'
  labelled.forEach(n => {
    const t = n.short.length > 14 ? n.short.slice(0, 13) + '…' : n.short
    ctx.fillText(t, n.x, n.y + n.r + 10)
  })
  ctx.restore()
}
