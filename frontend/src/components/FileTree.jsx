/**
 * FileTree — the repository's actual files, the way github.com lists them.
 *
 * Distinct from TreeView (HCT), which shows CODE structure (module › class ›
 * method) and therefore only ever contains parsed .py files. This one answers
 * "what is in this repo" — every file and folder, including READMEs, configs,
 * assets and anything else the author committed.
 */
import { useState, useMemo } from 'react'

// ── Flat "a/b/c.py" paths → nested folder structure ─────────────────────────
function buildTree(entries) {
  const root = { name: '', dirs: new Map(), files: [] }

  for (const e of entries) {
    const parts = e.path.split('/')
    const fileName = parts.pop()
    let node = root
    for (const part of parts) {
      if (!node.dirs.has(part)) {
        node.dirs.set(part, { name: part, dirs: new Map(), files: [] })
      }
      node = node.dirs.get(part)
    }
    node.files.push({
      name: fileName, size: e.size, link: e.link,
      collapsed: e.collapsed, path: e.path,
    })
  }
  return root
}

function fmtSize(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const ICONS = {
  py: '🐍', ipynb: '📓', js: '📜', jsx: '📜', ts: '📜', tsx: '📜',
  json: '⚙️', yml: '⚙️', yaml: '⚙️', toml: '⚙️', cfg: '⚙️', ini: '⚙️',
  md: '📝', rst: '📝', txt: '📝',
  png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', svg: '🖼️', ico: '🖼️',
  sh: '🔧', bat: '🔧', lock: '🔒',
}
function iconFor(name) {
  if (/^(license|licence)/i.test(name)) return '⚖️'
  if (/^dockerfile/i.test(name)) return '🐳'
  return ICONS[name.split('.').pop().toLowerCase()] || '📄'
}

// Count everything under a folder so a collapsed row still says how much it hides.
function countAll(node) {
  let n = node.files.length
  for (const d of node.dirs.values()) n += countAll(d)
  return n
}

function Folder({ node, depth, repoUrl, defaultOpen }) {
  const [open, setOpen] = useState(defaultOpen)
  const pad = depth * 14

  // GitHub orders folders before files, each alphabetical.
  const dirs  = useMemo(
    () => [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name)),
    [node],
  )
  const files = useMemo(
    () => [...node.files].sort((a, b) => a.name.localeCompare(b.name)),
    [node],
  )

  return (
    <>
      {depth >= 0 && (
        <div
          onClick={() => setOpen(o => !o)}
          style={{
            display: 'flex', alignItems: 'center', gap: 6,
            paddingLeft: pad, cursor: 'pointer', userSelect: 'none',
          }}
        >
          <span style={{ color: 'var(--txt3)', width: 10, flexShrink: 0 }}>
            {open ? '▾' : '▸'}
          </span>
          <span>📁</span>
          <span style={{ color: 'var(--accent)' }}>{node.name}</span>
          <span style={{ color: 'var(--txt3)', fontSize: '0.85em' }}>
            {countAll(node)}
          </span>
        </div>
      )}

      {open && (
        <>
          {dirs.map(d => (
            <Folder key={d.name} node={d} depth={depth + 1} repoUrl={repoUrl} />
          ))}
          {files.map(f => (
            <div
              key={f.path}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                paddingLeft: (depth + 1) * 14,
              }}
            >
              <span style={{ width: 10, flexShrink: 0 }} />
              <span>{f.collapsed != null ? '📁' : f.link ? '🔗' : iconFor(f.name)}</span>
              {repoUrl ? (
                <a
                  // GitHub serves directories under /tree/ and files
                  // under /blob/ — a collapsed vendored dir is a directory.
                  href={`${repoUrl}/${f.collapsed != null ? 'tree' : 'blob'}/HEAD/${f.path}`}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    color: 'var(--txt)', textDecoration: 'none',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}
                  title={f.path}
                >
                  {f.name}
                </a>
              ) : (
                <span style={{ color: 'var(--txt)' }}>{f.name}</span>
              )}
              <span style={{ marginLeft: 'auto', color: 'var(--txt3)', fontSize: '0.85em', flexShrink: 0 }}>
                {f.collapsed != null
                  ? `${f.collapsed} files · not expanded`
                  : f.link ? 'symlink' : fmtSize(f.size)}
              </span>
            </div>
          ))}
        </>
      )}
    </>
  )
}

export default function FileTree({
  fileTree, repoFullName = '', height = 220, fullscreen = false,
}) {
  const entries   = fileTree?.entries || []
  const truncated = !!fileTree?.truncated
  const total     = fileTree?.total ?? entries.length

  const root    = useMemo(() => buildTree(entries), [entries])
  const repoUrl = repoFullName ? `https://github.com/${repoFullName}` : ''

  const rootDirs = useMemo(
    () => [...root.dirs.values()].sort((a, b) => a.name.localeCompare(b.name)),
    [root],
  )
  const rootFiles = useMemo(
    () => [...root.files].sort((a, b) => a.name.localeCompare(b.name)),
    [root],
  )

  if (!entries.length) {
    return (
      <div style={{ color: 'var(--txt3)', fontSize: 12, padding: '8px 4px' }}>
        No file listing available
      </div>
    )
  }

  return (
    <div style={{
      fontFamily: 'var(--mono)',
      fontSize:   fullscreen ? 13 : 12,
      lineHeight: fullscreen ? 1.9 : 1.75,
      overflowY:  'auto',
      ...(fullscreen ? { height: '100%', paddingBottom: 24 } : { maxHeight: height }),
    }}>
      <div style={{ color: 'var(--txt3)', fontSize: 11, marginBottom: 6 }}>
        {total} file{total === 1 ? '' : 's'}
        {truncated && ` · showing first ${entries.length}`}
      </div>

      {/* Top level is rendered without a wrapper row so it isn't indented
          under a fake root folder. */}
      {rootDirs.map(d => (
        <Folder
          key={d.name}
          node={d}
          depth={0}
          repoUrl={repoUrl}
          // Open the first level by default in fullscreen, collapsed in the
          // small card where vertical space is scarce.
          defaultOpen={fullscreen}
        />
      ))}
      {rootFiles.map(f => (
        <div key={f.path} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 10, flexShrink: 0 }} />
          <span>{f.collapsed != null ? '📁' : f.link ? '🔗' : iconFor(f.name)}</span>
          {repoUrl ? (
            <a
              href={`${repoUrl}/${f.collapsed != null ? 'tree' : 'blob'}/HEAD/${f.path}`}
              target="_blank"
              rel="noreferrer"
              style={{
                color: 'var(--txt)', textDecoration: 'none',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}
              title={f.path}
            >
              {f.name}
            </a>
          ) : (
            <span style={{ color: 'var(--txt)' }}>{f.name}</span>
          )}
          <span style={{ marginLeft: 'auto', color: 'var(--txt3)', fontSize: '0.85em', flexShrink: 0 }}>
            {f.collapsed != null
              ? `${f.collapsed} files · not expanded`
              : f.link ? 'symlink' : fmtSize(f.size)}
          </span>
        </div>
      ))}

      {truncated && (
        <div style={{ color: 'var(--txt3)', fontSize: 11, marginTop: 8, paddingLeft: 16 }}>
          … {total - entries.length} more not shown
        </div>
      )}
    </div>
  )
}
