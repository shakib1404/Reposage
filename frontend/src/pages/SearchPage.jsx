import { useState, useRef, useCallback } from 'react'
import { searchRepos, uploadFiles } from '../api'

const EXAMPLES = [
  'Remove scratches from old photos',
  'Sentiment analysis of 3 Idiots movie reviews',
  'Colorize black and white images',
  'Super-resolution image upscaling',
  'Real-time face detection in video',
  'Text summarization of news articles',
]

const ACCEPT = '.png,.jpg,.jpeg,.gif,.bmp,.webp,.csv,.json,.pdf,.txt,.mp4,.wav,.mp3'

export default function SearchPage({ task, setTask, setRepos, setSuggestions, setJobId, setInputFiles, unlock, go }) {
  const [loading, setLoading] = useState(false)
  const [logs, setLogs] = useState([])
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState('')
  const [files, setFiles] = useState([])
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef()
  const fileRef = useRef()

  const addLog = (msg, type = '') => {
    const ts = new Date().toLocaleTimeString('en', { hour12: false })
    setLogs(prev => [...prev.slice(-30), { ts, msg, type }])
  }

  const handleFiles = useCallback((newFiles) => {
    const arr = Array.from(newFiles)
    setFiles(prev => {
      const existing = new Set(prev.map(f => f.name))
      return [...prev, ...arr.filter(f => !existing.has(f.name))]
    })
  }, [])

  const removeFile = (name) => {
    setFiles(prev => prev.filter(f => f.name !== name))
  }

  const onDrop = useCallback((e) => {
    e.preventDefault()
    setDragOver(false)
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files)
  }, [handleFiles])

  const doSearch = async () => {
    const t = inputRef.current.value.trim()
    if (!t) return
    setTask(t)
    setLogs([])
    setError('')
    setLoading(true)
    setProgress(5)

    try {
      // Step 1: Upload files if any
      let jobId = ''
      let uploadedNames = []
      if (files.length > 0) {
        addLog(`Uploading ${files.length} file(s)…`, 'info')
        setProgress(10)
        const uploadResult = await uploadFiles(t, files)
        jobId = uploadResult.job_id
        uploadedNames = uploadResult.uploaded_files || []
        setJobId(jobId)
        setInputFiles(uploadedNames)
        addLog(`✓ ${uploadedNames.length} file(s) uploaded — job ${jobId.slice(0,8)}…`, 'ok')
      } else {
        setJobId('')
        setInputFiles([])
      }

      // Steps 2-4: Search
      addLog('Extracting intent keywords…', 'info')
      setProgress(25)
      addLog('Querying GitHub via Serper…', 'info')
      setProgress(45)
      const data = await searchRepos(t)
      setProgress(75)
      if (data.query && data.query !== t.toLowerCase()) {
        addLog(`Interpreted as "${data.query}"`, 'info')
      }
      addLog(`Ranking candidates by relevance, popularity & runnability…`, 'info')
      setProgress(90)
      addLog(`Top ${data.repos.length} repos selected ✓`, 'ok')
      setProgress(100)
      setRepos(data.repos)
      setSuggestions?.(data.suggestions || [])
      unlock('select')
      setTimeout(() => go('select'), 500)
    } catch (e) {
      setError(e.message)
      addLog('Error: ' + e.message, 'warn')
    } finally {
      setLoading(false)
    }
  }

  const onKey = (e) => { if (e.key === 'Enter') doSearch() }

  const formatSize = (bytes) => {
    if (bytes < 1024) return bytes + ' B'
    if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB'
    return (bytes/(1024*1024)).toFixed(1) + ' MB'
  }

  return (
    <div style={{ maxWidth: 680, margin: '0 auto', padding: '56px 24px 48px' }} className="fade-in">
      <h2 style={{ fontSize: 30, fontWeight: 600, marginBottom: 8, letterSpacing: '-0.035em', lineHeight: 1.15 }}>
        What do you want to <span className="lp-grad-text">build</span>?
      </h2>
      <p style={{ color: 'var(--txt2)', fontSize: 14, marginBottom: 26, lineHeight: 1.6 }}>
        Describe your task in plain English. RepoSage finds a GitHub repo that solves it,
        maps the codebase, and runs it for you.
      </p>

      {/* Input */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        <input
          ref={inputRef}
          defaultValue={task}
          onKeyDown={onKey}
          placeholder="e.g. Remove scratches from an old photo"
          style={{
            flex: 1, padding: '13px 16px', border: '1px solid var(--border2)',
            borderRadius: 'var(--radius)', background: 'var(--bg2)', color: 'var(--txt)',
            fontFamily: 'var(--font)', fontSize: 14.5, outline: 'none',
            transition: 'border-color 0.15s, box-shadow 0.15s',
          }}
          onFocus={e => { e.target.style.borderColor = 'var(--accent)'; e.target.style.boxShadow = '0 0 0 3px rgba(93,142,255,0.14)' }}
          onBlur={e  => { e.target.style.borderColor = 'var(--border2)'; e.target.style.boxShadow = 'none' }}
        />
        <Btn onClick={doSearch} disabled={loading} primary>
          {loading ? <span className="spin" style={{ display:'inline-block',width:14,height:14,border:'2px solid rgba(255,255,255,0.3)',borderTopColor:'white',borderRadius:'50%' }}/> : <SearchIcon />}
          {loading ? 'Searching…' : 'Find repos'}
        </Btn>
      </div>

      {/* File upload zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => fileRef.current?.click()}
        style={{
          border: `2px dashed ${dragOver ? 'var(--accent)' : 'var(--border)'}`,
          borderRadius: 'var(--radius)',
          padding: files.length ? '10px 14px' : '20px 14px',
          textAlign: 'center',
          cursor: 'pointer',
          transition: 'all 0.2s',
          background: dragOver ? 'var(--accent-dim)' : 'var(--bg2)',
          marginBottom: 16,
        }}
      >
        <input
          ref={fileRef}
          type="file"
          multiple
          accept={ACCEPT}
          onChange={(e) => handleFiles(e.target.files)}
          style={{ display: 'none' }}
        />
        {files.length === 0 ? (
          <>
            <div style={{ fontSize: 24, marginBottom: 6, opacity: 0.6 }}>📎</div>
            <div style={{ fontSize: 13, color: 'var(--txt2)' }}>
              Drop files here or <span style={{ color: 'var(--accent)', textDecoration: 'underline' }}>browse</span>
            </div>
            <div style={{ fontSize: 11, color: 'var(--txt3)', marginTop: 4 }}>
              Images, CSVs, PDFs — optional input for the task
            </div>
          </>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }} onClick={e => e.stopPropagation()}>
            {files.map(f => (
              <div key={f.name} style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '4px 10px', background: 'var(--bg3)',
                borderRadius: 20, fontSize: 12,
              }}>
                <span style={{ color: 'var(--txt)' }}>{f.name}</span>
                <span style={{ color: 'var(--txt3)', fontSize: 11 }}>{formatSize(f.size)}</span>
                <button onClick={(e) => { e.stopPropagation(); removeFile(f.name) }}
                  style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14, lineHeight: 1, padding: 0 }}>
                  ×
                </button>
              </div>
            ))}
            <button onClick={(e) => { e.stopPropagation(); fileRef.current?.click() }}
              style={{ padding: '4px 12px', border: '1px dashed var(--border2)', borderRadius: 20, background: 'transparent', color: 'var(--txt2)', fontSize: 12, cursor: 'pointer' }}>
              + Add more
            </button>
          </div>
        )}
      </div>

      {/* Example chips */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 24 }}>
        {EXAMPLES.map(e => (
          <button key={e} onClick={() => { inputRef.current.value = e; setTask(e) }}
            style={{ padding: '4px 12px', border: '1px solid var(--border)', borderRadius: 20, background: 'transparent', color: 'var(--txt2)', fontSize: 12, cursor: 'pointer', transition: 'all 0.15s' }}
            onMouseOver={el => { el.target.style.borderColor='var(--border2)'; el.target.style.color='var(--txt)'; }}
            onMouseOut={el => { el.target.style.borderColor='var(--border)'; el.target.style.color='var(--txt2)'; }}>
            {e}
          </button>
        ))}
      </div>

      {/* Progress */}
      {loading && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ height: 3, background: 'var(--bg3)', borderRadius: 2, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: progress + '%', background: 'var(--accent)', borderRadius: 2, transition: 'width 0.4s ease' }} />
          </div>
        </div>
      )}

      {/* Logs */}
      {logs.length > 0 && (
        <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '10px 12px', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--txt2)', maxHeight: 160, overflowY: 'auto' }}>
          {logs.map((l, i) => (
            <div key={i} style={{ display:'flex', gap:8, color: l.type==='ok' ? 'var(--green)' : l.type==='warn' ? 'var(--yellow)' : l.type==='info' ? 'var(--accent)' : 'var(--txt2)' }}>
              <span style={{ opacity: 0.5, flexShrink: 0 }}>{l.ts}</span>
              <span>{l.msg}</span>
            </div>
          ))}
        </div>
      )}

      {error && <div style={{ marginTop: 10, color: 'var(--red)', fontSize: 12 }}>⚠ {error}</div>}

      {/* How it works */}
      <div style={{ marginTop: 36, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
        {[
          { icon: '🎯', title: 'Ranked shortlist',    desc: 'Nine Python repos scored on semantic fit, popularity and whether they can actually run' },
          { icon: '🌳', title: 'Structural analysis', desc: 'Code tree, call graph, dependency graph and the full file listing, built from the clone' },
          { icon: '🤖', title: 'Autonomous execution', desc: 'Installs, runs, reads the traceback, patches the files and retries until it passes' },
        ].map(c => (
          <div key={c.title} className="lp-feat" style={{ padding: '16px 18px' }}>
            <div style={{ fontSize: 20, marginBottom: 8 }}>{c.icon}</div>
            <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 5 }}>{c.title}</div>
            <div style={{ fontSize: 12, color: 'var(--txt2)', lineHeight: 1.55 }}>{c.desc}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function Btn({ children, onClick, disabled, primary }) {
  return (
    <button onClick={onClick} disabled={disabled} style={{
      padding: '12px 20px', border: `1px solid ${primary ? 'transparent' : 'var(--border2)'}`,
      borderRadius: 'var(--radius)', background: primary ? 'var(--grad)' : 'transparent',
      boxShadow: primary && !disabled ? '0 6px 20px -8px rgba(93,142,255,0.7)' : 'none',
      color: primary ? 'white' : 'var(--txt)', fontFamily: 'var(--font)', fontSize: 13,
      fontWeight: 500, cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.6 : 1,
      display: 'flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap',
    }}>{children}</button>
  )
}

function SearchIcon() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
}
