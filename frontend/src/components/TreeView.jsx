/**
 * TreeView — Hierarchical Code Tree (HCT)
 *
 * Renders: Package › Module › Class › Method
 * Supports compact (maxHeight prop) and fullscreen (height='auto') modes.
 */
import { useState } from 'react'

export default function TreeView({ modules = [], classes = [], height = 220, fullscreen = false }) {
  const [collapsedMods,  setCollapsedMods]  = useState({})
  const [collapsedCls,   setCollapsedCls]   = useState({})
  const [expandedDoc,    setExpandedDoc]    = useState({})
  const [expandedMethods, setExpandedMethods] = useState({})
  const [expandedFuncs,   setExpandedFuncs]   = useState({})

  const toggleMod     = k => setCollapsedMods(p   => ({ ...p, [k]: !p[k] }))
  const toggleCls     = k => setCollapsedCls(p    => ({ ...p, [k]: !p[k] }))
  const toggleDoc     = k => setExpandedDoc(p     => ({ ...p, [k]: !p[k] }))
  const toggleMethods = k => setExpandedMethods(p => ({ ...p, [k]: !p[k] }))
  const toggleFuncs   = k => setExpandedFuncs(p   => ({ ...p, [k]: !p[k] }))

  // Group classes by their module
  const clsByModule = {}
  ;(classes || []).forEach(c => {
    const key = c.module || c.module_short || ''
    ;(clsByModule[key] = clsByModule[key] || []).push(c)
  })

  // Deduplicate modules by path for display
  const seen = new Set()
  const dedupedMods = modules.filter(m => {
    if (seen.has(m.path || m.name)) return false
    seen.add(m.path || m.name)
    return true
  })

  const containerStyle = {
    fontFamily: 'var(--mono)',
    fontSize:   fullscreen ? 13 : 12,
    lineHeight: fullscreen ? 2.0 : 1.85,
    overflowY:  'auto',
    ...(fullscreen ? { height: '100%', paddingBottom: 24 } : { maxHeight: height }),
  }

  return (
    <div style={containerStyle}>
      {dedupedMods.length === 0 && (
        <div style={{ color: 'var(--txt3)', fontSize: 12, padding: '8px 4px' }}>
          No modules found
        </div>
      )}

      {dedupedMods.map(m => {
        const modKey   = m.name
        const isOpen   = !collapsedMods[modKey]
        const modCls   = clsByModule[m.module_short || m.short_name || modKey] || []
        const builtinCls = (m.classes || []).map(cname => ({ name: cname, method_names: [], methods: 0, bases: [], docstring: '' }))
        const allCls   = modCls.length ? modCls : builtinCls
        const modFuncs = m.functions || []
        const docStr   = m.docstring || ''
        const score    = typeof m.score === 'number' ? m.score.toFixed(1) : null

        return (
          <div key={modKey}>
            {/* ── Module row ─────────────────────────────────────────────── */}
            <div
              onClick={() => toggleMod(modKey)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                cursor: 'pointer', padding: '1px 4px', borderRadius: 4,
              }}
              onMouseOver={e => e.currentTarget.style.background = 'var(--bg3)'}
              onMouseOut={e  => e.currentTarget.style.background  = 'transparent'}
            >
              <ChevronIcon open={isOpen} />
              <FileIcon />
              <span style={{ color: 'var(--accent)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {m.path || (modKey + '.py')}
              </span>
              {score && (
                <ScorePill score={score} />
              )}
              {m.is_notebook && <span title="Jupyter notebook" style={{ fontSize: 11 }}>📓</span>}
            </div>

            {/* Module docstring (one line) */}
            {isOpen && docStr && fullscreen && (
              <div style={{ paddingLeft: 28, marginBottom: 2 }}>
                <span style={{ color: 'var(--txt3)', fontSize: 11, fontStyle: 'italic' }}>
                  {docStr.slice(0, 80)}{docStr.length > 80 ? '…' : ''}
                </span>
              </div>
            )}

            {/* ── Classes ────────────────────────────────────────────────── */}
            {isOpen && allCls.map(cls => {
              const clsKey  = `${modKey}.${cls.name}`
              const clsOpen = !collapsedCls[clsKey]
              const methods = cls.method_names || []
              const clsDoc  = cls.docstring || ''
              const bases   = (cls.bases || []).filter(b => b && b !== 'object').join(', ')

              return (
                <div key={clsKey}>
                  <div
                    onClick={() => toggleCls(clsKey)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      cursor: 'pointer', padding: '1px 4px 1px 20px', borderRadius: 4,
                    }}
                    onMouseOver={e => e.currentTarget.style.background = 'var(--bg3)'}
                    onMouseOut={e  => e.currentTarget.style.background  = 'transparent'}
                  >
                    <ChevronIcon open={clsOpen} small />
                    <ClassIcon />
                    <span style={{ color: 'var(--purple)', flex: 1 }}>
                      {cls.name}
                      {bases && <span style={{ color: 'var(--txt3)', fontSize: 10 }}> ({bases})</span>}
                    </span>
                    {methods.length > 0 && (
                      <span style={{ color: 'var(--txt3)', fontSize: 10 }}>{methods.length} methods</span>
                    )}
                  </div>

                  {/* Class docstring */}
                  {clsOpen && clsDoc && fullscreen && (
                    <div style={{ paddingLeft: 44, marginBottom: 2 }}>
                      <span style={{ color: 'var(--txt3)', fontSize: 11, fontStyle: 'italic' }}>
                        {clsDoc.slice(0, 72)}{clsDoc.length > 72 ? '…' : ''}
                      </span>
                    </div>
                  )}

                  {/* Methods */}
                  {clsOpen && (expandedMethods[clsKey] ? methods : methods.slice(0, fullscreen ? 20 : 4)).map(fn => (
                    <div
                      key={fn}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 6,
                        color: 'var(--green)', padding: '1px 4px 1px 40px',
                      }}
                    >
                      <FnIcon />
                      <span style={{ opacity: 0.8 }}>{fn}()</span>
                    </div>
                  ))}
                  {clsOpen && methods.length > (fullscreen ? 20 : 4) && (
                    <div
                      onClick={() => toggleMethods(clsKey)}
                      style={{
                        paddingLeft: 40, fontSize: 11,
                        color: 'var(--accent)', cursor: 'pointer',
                        textDecoration: 'underline', textDecorationStyle: 'dotted',
                        userSelect: 'none',
                      }}
                    >
                      {expandedMethods[clsKey]
                        ? '− collapse'
                        : `+${methods.length - (fullscreen ? 20 : 4)} more…`}
                    </div>
                  )}
                </div>
              )
            })}

            {/* ── Module-level functions ─────────────────────────────────── */}
            {isOpen && (expandedFuncs[modKey] ? modFuncs : modFuncs.slice(0, fullscreen ? 12 : 3)).map(fn => (
              <div
                key={fn}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  color: 'var(--green)', padding: '1px 4px 1px 22px',
                }}
              >
                <FnIcon />
                <span style={{ opacity: 0.8 }}>{fn}()</span>
              </div>
            ))}
            {isOpen && modFuncs.length > (fullscreen ? 12 : 3) && (
              <div
                onClick={() => toggleFuncs(modKey)}
                style={{
                  paddingLeft: 22, fontSize: 11,
                  color: 'var(--accent)', cursor: 'pointer',
                  textDecoration: 'underline', textDecorationStyle: 'dotted',
                  userSelect: 'none',
                }}
              >
                {expandedFuncs[modKey]
                  ? '− collapse'
                  : `+${modFuncs.length - (fullscreen ? 12 : 3)} functions…`}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Mini components ───────────────────────────────────────────────────────────
function ChevronIcon({ open, small = false }) {
  const s = small ? 9 : 10
  return (
    <span style={{ color: 'var(--txt3)', fontSize: s, flexShrink: 0, width: 10, display: 'inline-block', textAlign: 'center' }}>
      {open ? '▼' : '▶'}
    </span>
  )
}

function ScorePill({ score }) {
  const v   = parseFloat(score)
  const clr = v >= 8.5 ? 'var(--green)' : v >= 7 ? 'var(--yellow)' : 'var(--txt3)'
  return (
    <span style={{ fontSize: 10, color: clr, border: `1px solid color-mix(in srgb, ${clr} 27%, transparent)`, borderRadius: 10, padding: '0 5px', flexShrink: 0 }}>
      {score}
    </span>
  )
}

const FileIcon  = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
    <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
    <polyline points="13 2 13 9 20 9"/>
  </svg>
)

const ClassIcon = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--purple)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
    <rect x="2" y="3" width="20" height="14" rx="2"/>
    <line x1="8" y1="21" x2="16" y2="21"/>
    <line x1="12" y1="17" x2="12" y2="21"/>
  </svg>
)

const FnIcon = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--green)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
    <polyline points="16 18 22 12 16 6"/>
    <polyline points="8 6 2 12 8 18"/>
  </svg>
)
