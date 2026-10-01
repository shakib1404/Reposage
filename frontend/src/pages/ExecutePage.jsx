import { useState, useEffect, useRef } from 'react'
import { streamExecution, submitCredentials } from '../api'
import LoopFeed from '../components/LoopFeed'

export default function ExecutePage({ task, selectedRepo, analysis, setExecResult, jobId, setJobId, inputFiles, unlock, go }) {
  const [events, setEvents]   = useState([])
  const [metrics, setMetrics] = useState({ calls:0, tokens:0, iters:0, files:0 })
  const [tools, setTools]     = useState({})
  const [done, setDone]       = useState(false)
  const [summary, setSummary] = useState('')
  const [credFields, setCredFields]   = useState([])   // list of env var names needed
  const [credValues, setCredValues]   = useState({})   // {KEY: value}
  const [credSubmitting, setCredSubmitting] = useState(false)
  const cleanupRef = useRef(null)

  useEffect(() => {
    const cleanup = streamExecution(task, selectedRepo.full_name, analysis, (ev) => {
      if (ev.type === 'credential_needed') {
        setCredFields(ev.fields || [])
        setCredValues(Object.fromEntries((ev.fields || []).map(k => [k, ''])))
        return
      }
      if (ev.type === 'done') {
        setDone(true)
        if (ev.body) setSummary(ev.body)
        const resolvedJobId = ev.job_id || jobId
        if (resolvedJobId) setJobId(resolvedJobId)
        unlock('output')
        setExecResult({
            summary:      ev.body,
            metrics:      ev.metrics,
            job_id:       resolvedJobId,
            output:       ev.output       || '',
            run_script:   ev.run_script   || '',
            manual_guide: ev.manual_guide || '',
            returncode:   ev.returncode   ?? 0,
            fix_journal:  ev.fix_journal  || [],
            iterations:   ev.iterations   || 0,
            elapsed_s:    ev.elapsed_s    || 0,
          })
        return
      }
      setEvents(prev => [...prev, ev])
      // Merge, don't replace: an event that carries a partial metrics object
      // would otherwise blank out every field it omits.
      if (ev.metrics) setMetrics(prev => ({ ...prev, ...ev.metrics }))
      if (ev.tool) setTools(prev => ({ ...prev, [ev.tool]: (prev[ev.tool] || 0) + 1 }))
    }, jobId, inputFiles || [])
    cleanupRef.current = cleanup
    return () => cleanup?.()
  }, [])

  return (
    <>
    <div style={{ display:'flex', gap:0, height:'calc(100vh - 97px)', overflow:'hidden' }}>
      {/* Main feed */}
      <div style={{ flex:1, overflowY:'auto', padding:'20px 24px', borderRight:'1px solid var(--border)' }}>
        <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:12 }}>
          <h2 style={{ fontSize:16, fontWeight:600, flex:1 }}>Execution loop — {selectedRepo?.full_name}</h2>
          {!done && <span className="pulse" style={{ fontSize:12, color:'var(--green)' }}>● live</span>}
          {done && <span style={{ fontSize:12, color:'var(--green)' }}>✓ complete</span>}
        </div>

        {/* Progress bar */}
        <div style={{ height:3, background:'var(--bg3)', borderRadius:2, overflow:'hidden', marginBottom:16 }}>
          <div style={{ height:'100%', width: done?'100%': Math.min(events.length*12,90)+'%', background:'var(--green)', borderRadius:2, transition:'width 0.5s ease' }} />
        </div>

        <LoopFeed events={events} />

        {/* Done card */}
        {done && (
          <div className="fade-in" style={{ background:'var(--green-dim)', border:'1px solid var(--green)', borderRadius:'var(--radius-lg)', padding:'16px 18px', marginTop:12 }}>
            <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:8 }}>
              <span style={{ fontSize:16 }}>✅</span>
              <span style={{ fontWeight:600, color:'var(--green)' }}>Task completed</span>
            </div>
            <p style={{ fontSize:13, color:'var(--txt2)', lineHeight:1.6, marginBottom:12 }}>{summary}</p>
            <button onClick={()=>go('output')} style={{ padding:'8px 18px', background:'var(--accent)', border:'none', borderRadius:'var(--radius)', color:'white', fontSize:13, fontWeight:500, cursor:'pointer' }}>
              View output & dashboard →
            </button>
          </div>
        )}
      </div>

      {/* Sidebar */}
      <div style={{ width:220, flexShrink:0, overflowY:'auto', padding:'20px 16px' }}>
        <SideCard title="Metrics">
          {[
            { label:'LLM calls',   value: metrics.calls },
            { label:'Tokens',      value: metrics.tokens?.toLocaleString() },
            { label:'Iterations',  value: metrics.iters },
            // Backend sends repo_ctx["file_count"] here — every file in the
            // cloned workspace, not the ones the model opened. A repo with a
            // committed venv/ made this read "Files read 6,537".
            { label:'Workspace files', value: metrics.files?.toLocaleString() },
          ].map(m => (
            <div key={m.label} style={{ display:'flex', justifyContent:'space-between', fontSize:12, marginBottom:6 }}>
              <span style={{ color:'var(--txt2)' }}>{m.label}</span>
              <span style={{ fontWeight:500 }}>{m.value ?? 0}</span>
            </div>
          ))}
        </SideCard>

        {/* A "Context window" card used to sit here showing `ctx_tokens`,
            which the backend has never sent — it rendered a hardcoded 1.2k
            until the first metrics event, then "NaNk / 8k tokens" for the
            rest of the run. Removed rather than faked; the Tokens row above
            is a figure the executor actually measures. */}

        {Object.keys(tools).length > 0 && (
          <SideCard title="Tools used">
            {Object.entries(tools).map(([t, n]) => (
              <div key={t} style={{ fontSize:11, color:'var(--txt2)', marginBottom:4, display:'flex', justifyContent:'space-between' }}>
                <span>{t}</span>
                <span style={{ color:'var(--txt3)' }}>×{n}</span>
              </div>
            ))}
          </SideCard>
        )}

      </div>
    </div>

    {/* ── Credential modal ─────────────────────────────────────────────── */}
    {credFields.length > 0 && (
      <div style={{ position:'fixed', inset:0, background:'rgba(0,0,0,0.75)', zIndex:200, display:'flex', alignItems:'center', justifyContent:'center' }}>
        <div style={{ background:'var(--bg2)', border:'1px solid var(--border2)', borderRadius:'var(--radius-lg)', padding:'24px 28px', width:420, maxWidth:'90vw' }} className="fade-in">
          <div style={{ fontSize:16, fontWeight:600, marginBottom:6 }}>🔑 Credentials required</div>
          <div style={{ fontSize:12, color:'var(--txt2)', marginBottom:18, lineHeight:1.6 }}>
            This repo needs the following API keys or secrets to run.<br/>
            Values are only used for this execution and are not stored.
          </div>

          {credFields.map(key => (
            <div key={key} style={{ marginBottom:12 }}>
              <label style={{ fontSize:11, color:'var(--txt2)', display:'block', marginBottom:4, fontFamily:'var(--mono)' }}>{key}</label>
              <input
                type="password"
                placeholder={`Enter ${key}`}
                value={credValues[key] || ''}
                onChange={e => setCredValues(p => ({ ...p, [key]: e.target.value }))}
                style={{ width:'100%', padding:'8px 12px', border:'1px solid var(--border2)', borderRadius:'var(--radius)', background:'var(--bg3)', color:'var(--txt)', fontFamily:'var(--mono)', fontSize:13, outline:'none', boxSizing:'border-box' }}
              />
            </div>
          ))}

          <div style={{ display:'flex', gap:10, marginTop:20 }}>
            <button
              onClick={async () => {
                setCredSubmitting(true)
                try {
                  await submitCredentials(jobId, credValues)
                  setCredFields([])
                } catch(e) {
                  alert('Failed to submit: ' + e.message)
                } finally {
                  setCredSubmitting(false)
                }
              }}
              disabled={credSubmitting}
              style={{ flex:1, padding:'9px 0', background:'var(--accent)', border:'none', borderRadius:'var(--radius)', color:'white', fontSize:13, fontWeight:500, cursor:'pointer', opacity: credSubmitting ? 0.6 : 1 }}
            >
              {credSubmitting ? 'Submitting…' : 'Submit & Continue'}
            </button>
            <button
              onClick={async () => {
                // Submit empty values — executor will continue without them
                await submitCredentials(jobId, {}).catch(()=>{})
                setCredFields([])
              }}
              style={{ padding:'9px 14px', background:'transparent', border:'1px solid var(--border2)', borderRadius:'var(--radius)', color:'var(--txt2)', fontSize:13, cursor:'pointer' }}
            >
              Skip
            </button>
          </div>
        </div>
      </div>
    )}
    </>
  )
}

function SideCard({ title, children }) {
  return (
    <div style={{ background:'var(--bg2)', border:'1px solid var(--border)', borderRadius:'var(--radius)', padding:'10px 12px', marginBottom:10 }}>
      <div style={{ fontSize:10, fontWeight:500, color:'var(--txt3)', marginBottom:8, textTransform:'uppercase', letterSpacing:'0.07em' }}>{title}</div>
      {children}
    </div>
  )
}
