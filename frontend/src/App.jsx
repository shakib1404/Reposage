import { useState, useCallback, useEffect, useRef, Fragment } from 'react'
import SearchPage        from './pages/SearchPage'
import SelectPage        from './pages/SelectPage'
import AnalyzePage       from './pages/AnalyzePage'
import ArchitectPage     from './pages/ArchitectPage'
import ExecutePage       from './pages/ExecutePage'
import OutputPage        from './pages/OutputPage'
import TestPage          from './pages/TestPage'
import TaskExecPage      from './pages/TaskExecPage'
import CopyDetectPage   from './pages/CopyDetectPage'
import LandingPage       from './pages/LandingPage'
import HistoryPage       from './pages/HistoryPage'
import {
  getToken, setToken, clearToken, authMe,
  createHistory, updateHistory,
} from './api'

const STEPS = ['search', 'select', 'analyze', 'architect', 'execute', 'output', 'test', 'taskexec', 'copydetect', 'history']
const STEP_LABELS = {
  search:      '1. Find repos',
  select:      '2. Select repo',
  analyze:     '3. Analyze',
  architect:   '4. Architecture',
  execute:     '5. Execute',
  output:      '6. Output',
  test:        '7. Audit',
  taskexec:    '8. RepoTask Exec',
  copydetect:  '🔎 Copy Detector',
  history:     '📚 History',
}

// Steps that are always accessible regardless of workflow progress
const ALWAYS_ACCESSIBLE = new Set(['history', 'taskexec', 'copydetect'])

export default function App() {
  // ── Auth state ────────────────────────────────────────────────────────────
  const [user,      setUser]      = useState(null)
  const [authReady, setAuthReady] = useState(false)  // true once token check done

  // ── App state ─────────────────────────────────────────────────────────────
  const [step,         setStep]         = useState('search')
  const [unlocked,     setUnlocked]     = useState(['search'])
  const [task,         setTask]         = useState('')
  const [repos,        setRepos]        = useState([])
  const [suggestions,  setSuggestions]  = useState([])   // "refine your search" chips
  const [selectedRepo, setSelectedRepo] = useState(null)
  const [analysis,        setAnalysis]        = useState(null)
  const [architectResult, setArchitectResult] = useState(null)
  const [chatHistory,     setChatHistory]     = useState([])   // [{role,content}] — persists across tab switches
  const [execResult,      setExecResult]      = useState(null)
  const [testResult,   setTestResult]   = useState(null)
  const [jobId,        setJobId]        = useState('')
  const [inputFiles,   setInputFiles]   = useState([])
  const [historyId,    setHistoryId]    = useState('')  // current work's history _id

  // ── Verify token on load ───────────────────────────────────────────────────
  useEffect(() => {
    const token = getToken()
    if (!token) { setAuthReady(true); return }
    authMe()
      .then(u  => { setUser(u); setAuthReady(true) })
      .catch(() => { clearToken(); setAuthReady(true) })
  }, [])

  // ── Auth handler ───────────────────────────────────────────────────────────
  const handleAuth = (token, userData) => {
    setToken(token)
    setUser(userData)
  }

  // ── Drop every trace of the current piece of work ────────────────────────
  // Shared by "New task" and by signing out. Logging out has to clear the
  // same things: nothing here is re-fetched on login, so whatever is left in
  // state stays on screen for whoever signs in next — the previous account's
  // task would reappear in the header and prefill the search box.
  const resetWorkspace = () => {
    setTask('')
    setRepos([])
    setSuggestions([])
    setSelectedRepo(null)
    setAnalysis(null)
    setArchitectResult(null)
    setChatHistory([])
    setExecResult(null)
    setTestResult(null)
    setJobId('')
    setInputFiles([])
    setHistoryId('')
    historyIdRef.current    = ''
    taskRef.current         = ''
    jobIdRef.current        = ''
    analysisRepoRef.current = ''
    selectedRepoRef.current = null
    setUnlocked(['search'])
    setStep('search')
  }

  const handleLogout = () => {
    clearToken()
    setUser(null)
    resetWorkspace()
  }

  // ── New task — reset everything and go back to search ────────────────────
  const handleNewTask = resetWorkspace

  // ── Refs that are ALWAYS current — fix stale-closure bug ─────────────────
  // A plain function defined in the component body captures the state VALUE
  // from whichever render created it. Pages like SearchPage call unlock(x)
  // then `setTimeout(() => go(x), 500)` inside one async function — by the
  // time the timeout fires, App has re-rendered with the new `unlocked`
  // array, but the `go` closure captured by that setTimeout is still the
  // OLD one, so `unlocked.includes(x)` reads the stale pre-unlock value and
  // silently no-ops (unlocked tab shows a checkmark, but the view never
  // navigates). Using refs lets every callback always read the live value
  // regardless of when it was made.
  const userRef         = useRef(user)
  const taskRef         = useRef(task)
  const historyIdRef    = useRef(historyId)
  const jobIdRef        = useRef(jobId)
  const unlockedRef     = useRef(unlocked)
  const selectedRepoRef = useRef(selectedRepo)

  // Which repo the `analysis` currently in state was actually produced for.
  // Tracked separately from selectedRepo because the two legitimately
  // disagree for a moment while a new repo is being analysed — and because
  // SelectPage clears the selection to null when re-searching, so comparing
  // "previous selection vs new selection" would miss a repo change that went
  // through null in between.
  const analysisRepoRef = useRef('')

  useEffect(() => { userRef.current         = user         }, [user])
  useEffect(() => { taskRef.current         = task         }, [task])
  useEffect(() => { historyIdRef.current    = historyId    }, [historyId])
  useEffect(() => { jobIdRef.current        = jobId        }, [jobId])
  useEffect(() => { unlockedRef.current     = unlocked     }, [unlocked])
  useEffect(() => { selectedRepoRef.current = selectedRepo }, [selectedRepo])

  // ── Navigation helpers ────────────────────────────────────────────────────
  const unlock = useCallback(s => {
    setUnlocked(prev => prev.includes(s) ? prev : [...prev, s])
  }, [])

  const go = s => {
    if (unlockedRef.current.includes(s) || ALWAYS_ACCESSIBLE.has(s)) setStep(s)
  }

  // ── Stable history-save helpers (empty dep arrays — use refs inside) ─────

  const handleReposSet = useCallback(async (newRepos) => {
    setRepos(newRepos)
    const u = userRef.current
    const t = taskRef.current
    if (!u || !t) return
    try {
      const { history_id } = await createHistory(t, newRepos)
      setHistoryId(history_id)
      historyIdRef.current = history_id   // update ref immediately, don't wait for re-render
    } catch (e) {
      console.warn('[History] create failed:', e.message)
    }
  }, [])   // ← stable — no stale closures

  const handleRepoSelected = useCallback(async (repo) => {
    // Picking a DIFFERENT repo invalidates everything derived from the last
    // one. Without this the previous repo's analysis stays in state, and
    // AnalyzePage — whose fetch effect is `if (analysis) return; run()` — sees
    // a non-empty analysis on mount and never re-fetches, so it renders the
    // OLD repo's modules, graphs and metrics under the NEW repo's name. The
    // architecture diagram, execution output, audit report and codebase chat
    // are stale for exactly the same reason, so they all get cleared together.
    const newName = repo?.full_name || ''
    if (analysisRepoRef.current && newName && analysisRepoRef.current !== newName) {
      analysisRepoRef.current = ''
      setAnalysis(null)
      setArchitectResult(null)
      setExecResult(null)
      setTestResult(null)
      setChatHistory([])
      // Re-lock the steps whose data just went away — they unlock again as
      // the new repo moves through analyse → execute.
      setUnlocked(cur => cur.filter(s => s !== 'execute' && s !== 'output'))
    }

    setSelectedRepo(repo)
    unlock('analyze')
    unlock('architect')
    unlock('test')
    const hid = historyIdRef.current
    if (!hid) return
    updateHistory(hid, { selected_repo: repo, status: 'analyzing' })
      .catch(e => console.warn('[History] update(repo) failed:', e.message))
  }, [unlock])

  const handleAnalysisSet = useCallback(async (data) => {
    setAnalysis(data)
    // Remember which repo this analysis describes, so selecting a different
    // one later can detect that the cached analysis no longer applies.
    analysisRepoRef.current = selectedRepoRef.current?.full_name || ''
    const hid = historyIdRef.current
    if (!hid) return
    // Save full analysis — modules/classes/fcg/mdg for history tree view
    updateHistory(hid, { analysis: data, status: 'executing' })
      .catch(e => console.warn('[History] update(analysis) failed:', e.message))
  }, [])

  const handleArchitectResultSet = useCallback(async (data) => {
    setArchitectResult(data)
    const hid = historyIdRef.current
    if (!hid || !data) return
    updateHistory(hid, { architecture: data })
      .catch(e => console.warn('[History] update(architecture) failed:', e.message))
  }, [])

  const handleExecResultSet = useCallback(async (result) => {
    setExecResult(result)
    const hid = historyIdRef.current
    if (!hid) return
    updateHistory(hid, {
      execution: {
        summary:      result.summary      || '',
        returncode:   result.returncode   ?? 1,
        output:       (result.output      || '').slice(0, 8000),
        run_script:   result.run_script   || '',
        manual_guide: result.manual_guide || '',
        elapsed_s:    result.elapsed_s    || 0,
        iterations:   result.iterations   || 0,
        job_id:       result.job_id       || '',
        fix_journal:  result.fix_journal  || [],
      },
      job_id: result.job_id || jobIdRef.current || '',
      status: result.returncode === 0 ? 'completed' : 'failed',
    }).catch(e => console.warn('[History] update(execution) failed:', e.message))
  }, [])

  const handleTestResultSet = useCallback(async (result) => {
    setTestResult(result)
    const hid = historyIdRef.current
    if (!hid || !result) return
    updateHistory(hid, { audit: result, status: 'completed' })
      .catch(e => console.warn('[History] update(audit) failed:', e.message))
  }, [])

  // ── Restore from history ──────────────────────────────────────────────────
  const handleRestore = useCallback((state) => {
    if (state.task)         setTask(state.task)
    if (state.repos)        setRepos(state.repos)
    if (state.selectedRepo) {
      setSelectedRepo(state.selectedRepo)
      selectedRepoRef.current = state.selectedRepo   // needed before the check below
    }
    if (state.analysis)     setAnalysis(state.analysis)
    // Restored analysis belongs to the restored repo — record it so that
    // switching repos afterwards still detects the change and re-analyses.
    analysisRepoRef.current = state.analysis
      ? (state.selectedRepo?.full_name || '')
      : ''
    if (state.architecture) setArchitectResult(state.architecture)
    if (state.execResult)   setExecResult(state.execResult)
    if (state.testResult)   setTestResult(state.testResult)
    if (state.jobId)      { setJobId(state.jobId);      jobIdRef.current     = state.jobId }
    if (state.historyId)  { setHistoryId(state.historyId); historyIdRef.current = state.historyId }

    // Unlock all steps that have data
    const steps = ['search']
    if ((state.repos || []).length)  steps.push('select')
    if (state.selectedRepo)        { steps.push('analyze'); steps.push('architect'); steps.push('test') }
    if (state.analysis)              steps.push('execute')
    if (state.execResult)          { steps.push('output') }
    if (state.testResult && !steps.includes('test')) steps.push('test')
    setUnlocked(steps)
  }, [])

  // ─────────────────────────────────────────────────────────────────────────
  if (!authReady) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg)' }}>
        <div className="pulse" style={{ fontSize: 28, color: 'var(--accent)' }}>⌨</div>
      </div>
    )
  }

  if (!user) {
    return <LandingPage onAuth={handleAuth} />
  }

  const pageProps = {
    task, setTask,
    repos,           setRepos: handleReposSet,
    suggestions,     setSuggestions,
    selectedRepo,    setSelectedRepo: handleRepoSelected,
    analysis,        setAnalysis: handleAnalysisSet,
    architectResult, setArchitectResult: handleArchitectResultSet,
    chatHistory,     setChatHistory,
    execResult,      setExecResult: handleExecResultSet,
    testResult,      setTestResult: handleTestResultSet,
    jobId,           setJobId,
    inputFiles,      setInputFiles,
    unlock, go,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      {/* ── Header + tabs ──────────────────────────────────────────────── */}
      <div className="app-head">
        <header style={{
          padding: '11px 18px',
          display: 'flex', alignItems: 'center', gap: 12,
        }}>
          <div style={{
            width: 30, height: 30, borderRadius: 9, background: 'var(--grad)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
            boxShadow: '0 4px 14px -5px rgba(93,142,255,0.75)',
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
            </svg>
          </div>
          <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.025em' }}>RepoSage</span>
          <span style={{ color: 'var(--txt3)', fontSize: 12, borderLeft: '1px solid var(--border2)', paddingLeft: 12 }}>
            Autonomous repo exploration &amp; execution
          </span>
          <div style={{ flex: 1 }} />

          {/* Current task, so you always know what this session is about */}
          {task && (
            <span
              title={task}
              style={{
                fontSize: 12, color: 'var(--txt2)', maxWidth: 260,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                background: 'var(--bg2)', border: '1px solid var(--border)',
                borderRadius: 20, padding: '4px 12px',
              }}
            >
              {task}
            </span>
          )}

          <button
            onClick={handleNewTask}
            className="lp-btn lp-btn-primary"
            style={{ padding: '7px 14px', fontSize: 12.5 }}
            title="Start a fresh task"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            New task
          </button>

          {/* User badge */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              fontSize: 12, color: 'var(--txt2)',
              background: 'var(--bg2)', border: '1px solid var(--border)',
              borderRadius: 20, padding: '4px 6px 4px 4px',
              display: 'flex', alignItems: 'center', gap: 7,
            }}>
              <span style={{
                width: 20, height: 20, borderRadius: '50%', flexShrink: 0,
                background: 'var(--grad)', color: '#fff',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase',
              }}>
                {(user.username || '?').slice(0, 1)}
              </span>
              <span style={{ fontWeight: 600, color: 'var(--txt)', paddingRight: 4 }}>{user.username}</span>
            </div>
            <button
              onClick={handleLogout}
              className="lp-btn lp-btn-ghost"
              style={{ padding: '6px 12px', fontSize: 12, fontWeight: 500, color: 'var(--txt2)' }}
            >
              Sign out
            </button>
          </div>
        </header>

        {/* ── Step tabs ────────────────────────────────────────────────── */}
        <nav className="app-tabs">
          {STEPS.map(s => {
            const always   = ALWAYS_ACCESSIBLE.has(s)
            const isActive = step === s
            const isDone   = unlocked.includes(s) && !isActive && !always
            const isLocked = !unlocked.includes(s) && !always

            const cls = ['app-tab',
              isActive ? 'active' : '',
              isDone   ? 'done'   : '',
              always   ? 'util'   : ''].filter(Boolean).join(' ')

            return (
              <Fragment key={s}>
                {/* Divider between the linear workflow and the always-on tools */}
                {s === 'copydetect' && <span className="app-tab-sep" />}
                <button onClick={() => go(s)} disabled={isLocked} className={cls}>
                  {isDone && (
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="20 6 9 17 4 12"/>
                    </svg>
                  )}
                  {isLocked && (
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                    </svg>
                  )}
                  {STEP_LABELS[s]}
                </button>
              </Fragment>
            )
          })}
        </nav>
      </div>

      {/* ── Page content ───────────────────────────────────────────────── */}
      <main style={{ flex: 1, overflow: 'auto' }}>
        {step === 'history'     && <HistoryPage onRestore={handleRestore} onNew={handleNewTask} go={s => { go(s); setStep(s) }} />}
        {step === 'copydetect'  && <CopyDetectPage />}
        {step !== 'history' && step !== 'copydetect' && (
          <>
            {step === 'search'    && <SearchPage    {...pageProps} />}
            {step === 'select'    && <SelectPage    {...pageProps} />}
            {step === 'analyze'   && <AnalyzePage   {...pageProps} />}
            {step === 'architect' && <ArchitectPage {...pageProps} />}
            {step === 'execute'   && <ExecutePage   {...pageProps} />}
            {step === 'output'    && <OutputPage    {...pageProps} />}
            {step === 'test'      && <TestPage      {...pageProps} />}
            {step === 'taskexec'  && <TaskExecPage  {...pageProps} />}
          </>
        )}
      </main>
    </div>
  )
}
