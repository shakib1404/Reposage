import { useState, useRef, useEffect } from 'react'
import { jsPDF } from 'jspdf'
import { streamTest, streamAutofix } from '../api'

// ── Scanner metadata ─────────────────────────────────────────────────────────
const SCANNERS = [
  { id: 'lint',     name: 'Linting',          icon: '🔍', tool: 'ruff'           },
  { id: 'security', name: 'Security',         icon: '🔒', tool: 'bandit'         },
  { id: 'deps',     name: 'Dependency CVEs',  icon: '📦', tool: 'pip-audit'      },
  { id: 'types',    name: 'Type Analysis',    icon: '🔬', tool: 'mypy'           },
  { id: 'secrets',  name: 'Secret Detection', icon: '🔑', tool: 'detect-secrets' },
  { id: 'deadcode', name: 'Dead Code',        icon: '💀', tool: 'vulture'        },
  { id: 'semgrep',  name: 'Pattern Analysis', icon: '🧩', tool: 'semgrep'        },
]

// ── Severity config ───────────────────────────────────────────────────────────
const SEV = {
  critical: { color: '#f87171', bg: 'rgba(248,113,113,0.12)', label: 'Critical' },
  high:     { color: '#fb923c', bg: 'rgba(251,146,60,0.12)',  label: 'High'     },
  medium:   { color: '#fbbf24', bg: 'rgba(251,191,36,0.12)',  label: 'Medium'   },
  low:      { color: '#34d399', bg: 'rgba(52,211,153,0.12)',  label: 'Low'      },
  info:     { color: '#8888a0', bg: 'rgba(136,136,160,0.12)', label: 'Info'     },
}

const GRADE_STYLE = {
  A: { color: '#34d399', bg: 'rgba(52,211,153,0.15)',   border: '#34d399' },
  B: { color: '#5d8eff', bg: 'rgba(93,142,255,0.15)',   border: '#5d8eff' },
  C: { color: '#fbbf24', bg: 'rgba(251,191,36,0.15)',   border: '#fbbf24' },
  D: { color: '#fb923c', bg: 'rgba(251,146,60,0.15)',   border: '#fb923c' },
  F: { color: '#f87171', bg: 'rgba(248,113,113,0.15)',  border: '#f87171' },
}

// Short scanner labels for the PDF's compact rows — jsPDF's built-in fonts
// only support WinAnsi encoding, and the full names ("Dependency CVEs",
// "Secret Detection") don't fit the narrow column without an ugly mid-word
// cut, so we use a dedicated short form there instead of slicing sc.name.
const SCANNER_SHORT = {
  lint: 'Lint', security: 'Security', deps: 'Dep CVEs',
  types: 'Types', secrets: 'Secrets', deadcode: 'Dead Code', semgrep: 'Semgrep',
}

// Per-finding weight = midpoint of the finding's CVSS v3.1 qualitative
// severity band (FIRST.org / NIST NVD standard: None 0.0, Low 0.1-3.9,
// Medium 4.0-6.9, High 7.0-8.9, Critical 9.0-10.0). Must match backend
// tester.py's CVSS_WEIGHTS — used here only to *display* each severity's
// share of risk exposure; the score itself always comes from the backend.
const CVSS_WEIGHTS = { critical: 9.5, high: 8.0, medium: 5.5, low: 2.0, info: 0.0 }

// ─────────────────────────────────────────────────────────────────────────────

export default function TestPage({ selectedRepo, jobId, setTestResult, unlock, go }) {
  const [phase, setPhase]           = useState('idle')   // idle | running | done | error
  const [scanStatus, setScanStatus] = useState({})       // {id: 'pending'|'running'|'done'|'error'}
  const [scanCounts, setScanCounts] = useState({})       // {id: n}
  const [log, setLog]               = useState([])       // [{text, type}]
  const [report, setReport]         = useState(null)     // final report object
  const [activeTab, setActiveTab]   = useState('findings')  // findings | scanner | raw
  const [sevFilter, setSevFilter]   = useState('all')
  const [scanFilter, setScanFilter] = useState('all')
  const [expandedFinding, setExpandedFinding] = useState(null)
  const [autofixPhase, setAutofixPhase] = useState('idle')   // idle | running | done | nofix | error
  const [autofixLog, setAutofixLog]     = useState([])       // [{text, type}]
  const [autofixResult, setAutofixResult] = useState(null)   // {pr_url, branch, files_changed, fixes}
  const cleanupRef = useRef(null)
  const autofixCleanupRef = useRef(null)
  const logEndRef  = useRef(null)

  function downloadReport() {
    if (!report) return

    const PW = 210   // page width
    const PH = 297   // page height
    const ML = 14    // margin left
    const MR = 14    // margin right
    const CW = PW - ML - MR  // content width
    const MAX_PAGES = 2   // keep the summary short — an index, not a full dump

    const SEV_RGB = {
      critical: [248, 113, 113],
      high:     [251, 146,  60],
      medium:   [251, 191,  36],
      low:      [ 52, 211, 153],
      info:     [136, 136, 160],
    }
    const GRADE_RGB = {
      A: [52, 211, 153], B: [93, 142, 255],
      C: [251, 191, 36], D: [251, 146, 60], F: [248, 113, 113],
    }
    const sevRank = { critical: 0, high: 1, medium: 2, low: 3, info: 4 }

    const slug        = (report.repo || 'report').replace(/\//g, '_')
    const summaryFile  = `audit_summary_${slug}.pdf`
    const detailsFile  = `audit_details_${slug}.pdf`

    // ── Group findings into issue types (same scanner + rule) ────────────────
    // The summary links to a group, not to every individual occurrence — that
    // keeps the summary short while still getting you straight to everything
    // related to a given error in the details PDF.
    const groupMap = new Map()
    for (const f of (report.findings || [])) {
      const key = `${f.scanner}::${f.rule || f.message}`
      let g = groupMap.get(key)
      if (!g) {
        g = { scanner: f.scanner, rule: f.rule, severity: f.severity, message: f.message, items: [] }
        groupMap.set(key, g)
      }
      g.items.push(f)
      if (sevRank[f.severity] < sevRank[g.severity]) g.severity = f.severity
    }
    const groups = [...groupMap.values()].sort((a, b) =>
      (sevRank[a.severity] ?? 5) - (sevRank[b.severity] ?? 5) || b.items.length - a.items.length)

    // ══════════════════════════════════════════════════════════════════════
    // 1) DETAILS PDF — every occurrence, grouped by issue type. Unbounded
    //    length (it's the appendix), but styled to match the summary.
    // ══════════════════════════════════════════════════════════════════════
    const details = new jsPDF({ unit: 'mm', format: 'a4' })
    let dy = 0

    function dNewPage() { details.addPage(); dy = 18 }
    function dCheck(needed = 10) { if (dy + needed > PH - 14) dNewPage() }

    details.setFillColor(15, 15, 17)
    details.rect(0, 0, PW, 20, 'F')
    details.setTextColor(232, 232, 240)
    details.setFontSize(12)
    details.setFont('helvetica', 'bold')
    details.text('Code Audit — Detailed Findings', ML, 9)
    details.setFontSize(7.5)
    details.setFont('helvetica', 'normal')
    details.setTextColor(136, 136, 160)
    details.text(report.repo || '', ML, 15)
    details.text('RepoSage', PW - MR, 15, { align: 'right' })
    dy = 26

    details.setFont('helvetica', 'italic')
    details.setFontSize(7.5)
    details.setTextColor(130, 130, 150)
    details.text(`Companion to ${summaryFile} — keep both files in the same folder for the "view »" links to work.`, ML, dy)
    dy += 9

    groups.forEach((g, gi) => {
      dCheck(18)
      // Record the page this group starts on, so the summary can link to it.
      g.page = details.internal.getCurrentPageInfo().pageNumber

      const [r, gg, b] = SEV_RGB[g.severity] || [136, 136, 160]
      details.setFillColor(r, gg, b)
      details.roundedRect(ML, dy - 4.5, CW, 7.5, 1.2, 1.2, 'F')
      details.setTextColor(255, 255, 255)
      details.setFontSize(8)
      details.setFont('helvetica', 'bold')
      const sc = SCANNERS.find(s => s.id === g.scanner)
      details.text(`${g.severity.toUpperCase()}  ·  ${SCANNER_SHORT[g.scanner] || sc?.name || g.scanner}${g.rule ? '  ·  ' + g.rule : ''}`, ML + 2.5, dy)
      details.text(`${g.items.length} occurrence${g.items.length !== 1 ? 's' : ''}`, PW - MR - 2, dy, { align: 'right' })
      dy += 7

      details.setFont('helvetica', 'normal')
      details.setFontSize(7.5)
      details.setTextColor(60, 60, 75)
      const msgLines = details.splitTextToSize(g.message || '', CW - 4)
      details.text(msgLines.slice(0, 2), ML + 1, dy)
      dy += Math.min(msgLines.length, 2) * 3.8 + 4

      g.items.forEach((f, i) => {
        dCheck(6)
        if (i % 2 === 0) {
          details.setFillColor(246, 246, 249)
          details.rect(ML, dy - 3.2, CW, 5.6, 'F')
        }
        details.setFont('helvetica', 'normal')
        details.setFontSize(7)
        details.setTextColor(90, 90, 105)
        const loc = (f.file || '') + (f.line ? `:${f.line}` : '')
        details.text(loc.length > 70 ? '…' + loc.slice(-69) : (loc || '—'), ML + 1, dy)
        if (f.confidence) {
          details.setTextColor(93, 142, 255)
          details.text(String(f.confidence), PW - MR, dy, { align: 'right' })
        }
        dy += 5.6
      })

      dy += 4
      if (gi < groups.length - 1) {
        details.setDrawColor(228, 228, 233)
        details.setLineWidth(0.15)
        details.line(ML, dy - 2.5, PW - MR, dy - 2.5)
      }
    })

    const dPageCount = details.getNumberOfPages()
    for (let i = 1; i <= dPageCount; i++) {
      details.setPage(i)
      details.setFontSize(7)
      details.setTextColor(160, 160, 180)
      details.text(`Page ${i} of ${dPageCount}`, PW / 2, PH - 6, { align: 'center' })
    }

    // ══════════════════════════════════════════════════════════════════════
    // 2) SUMMARY PDF — header / grade / severity / scanner breakdown, then a
    //    clickable issue index (max MAX_PAGES pages) linking into the
    //    details PDF above.
    // ══════════════════════════════════════════════════════════════════════
    const doc = new jsPDF({ unit: 'mm', format: 'a4' })
    let y = 0
    let pageNum = 1

    // Advances to a new page only if one is still allowed; returns false once
    // the page budget (MAX_PAGES) is exhausted, so callers know to stop.
    function ensureRoom(needed = 12) {
      if (y + needed <= PH - 12) return true
      if (pageNum >= MAX_PAGES) return false
      doc.addPage()
      pageNum++
      y = 16
      return true
    }
    function hline(yy, r = 200, g = 200, b = 200) {
      doc.setDrawColor(r, g, b)
      doc.setLineWidth(0.2)
      doc.line(ML, yy, PW - MR, yy)
    }

    // ── Header bar ──────────────────────────────────────────────────────────
    doc.setFillColor(15, 15, 17)
    doc.rect(0, 0, PW, 22, 'F')
    doc.setTextColor(232, 232, 240)
    doc.setFontSize(13)
    doc.setFont('helvetica', 'bold')
    doc.text('Code Audit Report', ML, 10)
    doc.setFontSize(8)
    doc.setFont('helvetica', 'normal')
    doc.setTextColor(136, 136, 160)
    doc.text(`Generated: ${new Date().toLocaleString()}`, ML, 16)
    doc.text('RepoSage', PW - MR, 16, { align: 'right' })
    y = 28

    // ── Repo + grade ─────────────────────────────────────────────────────────
    doc.setTextColor(30, 30, 40)
    doc.setFontSize(11)
    doc.setFont('helvetica', 'bold')
    doc.text(report.repo || '', ML, y)

    const gr = report.grade || 'F'
    const [gr_r, gr_g, gr_b] = GRADE_RGB[gr] || [200, 200, 200]
    doc.setFillColor(gr_r, gr_g, gr_b)
    doc.roundedRect(PW - MR - 22, y - 8, 22, 12, 2, 2, 'F')
    doc.setTextColor(255, 255, 255)
    doc.setFontSize(9)
    doc.setFont('helvetica', 'bold')
    doc.text(`${gr}  ${report.score}/100`, PW - MR - 11, y - 1, { align: 'center' })
    y += 6

    doc.setFont('helvetica', 'normal')
    doc.setFontSize(8)
    doc.setTextColor(100, 100, 120)
    doc.text(`${report.total || 0} total findings  ·  ${report.elapsed_s || 0}s`, ML, y)
    y += 8
    hline(y); y += 6

    // ── Severity summary ──────────────────────────────────────────────────────
    doc.setFontSize(8)
    doc.setFont('helvetica', 'bold')
    doc.setTextColor(80, 80, 100)
    doc.text('SEVERITY SUMMARY', ML, y)
    y += 5

    const sevOrder = ['critical', 'high', 'medium', 'low', 'info']
    const boxW = CW / 5
    sevOrder.forEach((sev, i) => {
      const cnt = (report.severity || {})[sev] || 0
      const [r, g, b] = SEV_RGB[sev]
      const bx = ML + i * boxW
      doc.setFillColor(r, g, b, 0.15)
      doc.setDrawColor(r, g, b)
      doc.setLineWidth(0.4)
      doc.roundedRect(bx, y, boxW - 2, 14, 1, 1, 'FD')
      doc.setTextColor(r, g, b)
      doc.setFontSize(13)
      doc.setFont('helvetica', 'bold')
      doc.text(String(cnt), bx + (boxW - 2) / 2, y + 7, { align: 'center' })
      doc.setFontSize(6.5)
      doc.setFont('helvetica', 'normal')
      doc.text(sev.charAt(0).toUpperCase() + sev.slice(1), bx + (boxW - 2) / 2, y + 12, { align: 'center' })
    })
    y += 20
    hline(y); y += 6

    // ── Scanner breakdown ─────────────────────────────────────────────────────
    doc.setFontSize(8)
    doc.setFont('helvetica', 'bold')
    doc.setTextColor(80, 80, 100)
    doc.text('SCANNER BREAKDOWN', ML, y)
    y += 5

    SCANNERS.forEach(sc => {
      const cnt = (report.by_scanner || {})[sc.id] || 0
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(8)
      doc.setTextColor(50, 50, 60)
      doc.text(`${sc.name}`, ML + 2, y)
      // bar
      const barX = ML + 50, barW = CW - 60, barH = 3
      doc.setFillColor(230, 230, 235)
      doc.roundedRect(barX, y - 3, barW, barH, 0.5, 0.5, 'F')
      if (cnt > 0) {
        const maxCnt = Math.max(...SCANNERS.map(s => (report.by_scanner || {})[s.id] || 0), 1)
        const fill = (cnt / maxCnt) * barW
        doc.setFillColor(93, 142, 255)
        doc.roundedRect(barX, y - 3, fill, barH, 0.5, 0.5, 'F')
      }
      doc.setTextColor(cnt > 0 ? 93 : 120, cnt > 0 ? 142 : 120, cnt > 0 ? 255 : 120)
      doc.text(String(cnt), PW - MR, y, { align: 'right' })
      y += 5
    })
    y += 1
    hline(y); y += 7

    // ── Issue index (compact, one row per issue type, links into the
    //    details PDF; capped to MAX_PAGES to keep the summary short) ─────────
    const totalFindings = (report.findings || []).length
    doc.setFontSize(8)
    doc.setFont('helvetica', 'bold')
    doc.setTextColor(80, 80, 100)
    doc.text(
      `ISSUE INDEX  (${groups.length} issue type${groups.length !== 1 ? 's' : ''} · ${totalFindings} finding${totalFindings !== 1 ? 's' : ''} — tap a row to open it in the details PDF)`,
      ML, y)
    y += 6

    const ROW_H = 6.2
    let lastSev = null
    let shown = 0
    for (const g of groups) {
      const needsHeader = g.severity !== lastSev
      if (!ensureRoom(ROW_H + (needsHeader ? 5 : 0))) break

      // Severity section header when it changes
      if (needsHeader) {
        const [r, g_, b] = SEV_RGB[g.severity] || [136, 136, 160]
        doc.setFillColor(r, g_, b)
        doc.roundedRect(ML, y - 3.5, CW, 5, 1, 1, 'F')
        doc.setTextColor(255, 255, 255)
        doc.setFontSize(6.5)
        doc.setFont('helvetica', 'bold')
        doc.text(g.severity.toUpperCase(), ML + 2, y)
        y += 5
        lastSev = g.severity
      }

      const rowTop = y - 3.5

      // Row background (alternating)
      if (shown % 2 === 0) {
        doc.setFillColor(246, 246, 249)
        doc.rect(ML, rowTop, CW, ROW_H, 'F')
      }

      doc.setFont('helvetica', 'bold')
      doc.setFontSize(7)
      doc.setTextColor(80, 80, 100)
      doc.text(SCANNER_SHORT[g.scanner] || g.scanner, ML + 1.5, y + 0.7)

      const countW = 18
      const linkW  = 16
      const msgX   = ML + 25
      const msgW   = CW - 25 - countW - linkW - 3
      doc.setFont('helvetica', 'normal')
      doc.setFontSize(7)
      doc.setTextColor(40, 40, 50)
      const label = g.rule ? `${g.rule} — ${g.message}` : g.message
      const oneLine = doc.splitTextToSize(label || '', msgW)[0] || ''
      doc.text(oneLine, msgX, y + 0.7)

      doc.setFont('helvetica', 'normal')
      doc.setFontSize(6.5)
      doc.setTextColor(120, 120, 140)
      doc.text(`×${g.items.length}`, PW - MR - linkW - 2, y + 0.7, { align: 'right' })

      // Clickable link — jumps straight to this issue's section in the
      // details PDF (relative file link with a #page= anchor).
      doc.setFont('helvetica', 'bold')
      doc.setFontSize(6.5)
      doc.setTextColor(93, 142, 255)
      doc.text('view »', PW - MR, y + 0.7, { align: 'right' })
      doc.link(ML, rowTop, CW, ROW_H, { url: `${detailsFile}#page=${g.page}` })

      y += ROW_H
      shown++
    }

    const remaining = groups.length - shown
    if (remaining > 0 && ensureRoom(7)) {
      doc.setFont('helvetica', 'italic')
      doc.setFontSize(7.5)
      doc.setTextColor(120, 120, 140)
      doc.text(
        `+ ${remaining} more issue type(s) not shown — open ${detailsFile} for the full list.`,
        ML, y + 3)
      y += 7
    }

    // ── Footer note + page numbers on every page ──────────────────────────────
    const pageCount = doc.getNumberOfPages()
    for (let i = 1; i <= pageCount; i++) {
      doc.setPage(i)
      doc.setFontSize(6.5)
      doc.setTextColor(170, 170, 190)
      doc.text(`Full details: ${detailsFile}`, ML, PH - 6)
      doc.setFontSize(7)
      doc.setTextColor(160, 160, 180)
      doc.text(`Page ${i} of ${pageCount}`, PW / 2, PH - 6, { align: 'center' })
    }

    // Save both. The details PDF first, then the summary a beat later —
    // browsers can silently block a second simultaneous download from the
    // same click handler otherwise.
    details.save(detailsFile)
    setTimeout(() => doc.save(summaryFile), 350)
  }

  useEffect(() => {
    // Auto-scroll log
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  function startAudit() {
    if (!selectedRepo) return
    setPhase('running')
    setLog([])
    setReport(null)
    setScanStatus({})
    setScanCounts({})

    const cleanup = streamTest(selectedRepo.full_name, jobId || '', (ev) => {
      if (ev.type === 'error') {
        setPhase('error')
        addLog(ev.body || 'Unknown error', 'error')
        return
      }

      if (ev.type === 'status') {
        addLog(ev.title + (ev.body ? ` — ${ev.body}` : ''), 'status')
        return
      }

      if (ev.type === 'warning') {
        addLog('⚠ ' + ev.title + ': ' + ev.body, 'warning')
        return
      }

      if (ev.type === 'scanner_start') {
        setScanStatus(p => ({ ...p, [ev.scanner]: 'running' }))
        addLog(`${ev.icon} ${ev.title}: running…`, 'scan')
        return
      }

      if (ev.type === 'scanner_done') {
        setScanStatus(p => ({ ...p, [ev.scanner]: 'done' }))
        setScanCounts(p => ({ ...p, [ev.scanner]: (ev.findings || []).length }))
        addLog(`${ev.icon} ${ev.title}: ${ev.body}`, 'done')
        return
      }

      if (ev.type === 'scanner_error') {
        setScanStatus(p => ({ ...p, [ev.scanner]: 'error' }))
        addLog(`${ev.icon} ${ev.title}: ⚠ ${ev.body}`, 'error')
        return
      }

      if (ev.type === 'done' && ev.report) {
        setReport(ev.report)
        setPhase('done')
        setTestResult(ev.report)
        unlock('test')
        addLog('✓ Audit complete — ' + ev.title, 'done')
      }
    })

    cleanupRef.current = cleanup
  }

  function stopAudit() {
    cleanupRef.current?.()
    setPhase('idle')
    addLog('Audit stopped by user.', 'warning')
  }

  function addLog(text, type = 'status') {
    setLog(prev => [...prev, { text, type }])
  }

  // ── Semgrep autofix → pull request ──────────────────────────────────────────

  function startAutofix() {
    if (!selectedRepo) return
    const ok = window.confirm(
      `This runs Semgrep's autofix on ${selectedRepo.full_name}, then pushes a new branch ` +
      `(forking the repo first if needed) and opens a pull request for review. It never commits ` +
      `to the default branch directly. Continue?`
    )
    if (!ok) return

    setAutofixPhase('running')
    setAutofixLog([])
    setAutofixResult(null)

    const cleanup = streamAutofix(selectedRepo.full_name, jobId || '', (ev) => {
      if (ev.type === 'error') {
        setAutofixPhase('error')
        addAutofixLog(ev.body || 'Unknown error', 'error')
        return
      }
      if (ev.type === 'status') {
        addAutofixLog(ev.title + (ev.body ? ` — ${ev.body}` : ''), 'status')
        return
      }
      if (ev.type === 'done') {
        if (ev.pr_url) {
          setAutofixPhase('done')
          setAutofixResult(ev)
          addAutofixLog('✓ Pull request opened', 'done')
        } else {
          setAutofixPhase('nofix')
          addAutofixLog(ev.body || 'No autofixable findings.', 'status')
        }
      }
    })

    autofixCleanupRef.current = cleanup
  }

  function stopAutofix() {
    autofixCleanupRef.current?.()
    setAutofixPhase('idle')
    addAutofixLog('Autofix stopped by user.', 'warning')
  }

  function addAutofixLog(text, type = 'status') {
    setAutofixLog(prev => [...prev, { text, type }])
  }

  // ── Findings helpers ────────────────────────────────────────────────────────

  const findings = report?.findings || []
  const filtered = findings.filter(f => {
    if (sevFilter !== 'all' && f.severity !== sevFilter) return false
    if (scanFilter !== 'all' && f.scanner !== scanFilter) return false
    return true
  })

  const sevCounts = report?.severity || {}
  const totalFindings = report?.total || 0

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 97px)', overflow: 'hidden' }}>

      {/* ── Left panel: scanner status + log ──────────────────────────────── */}
      <div style={{ width: 280, flexShrink: 0, borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>

        {/* Header */}
        <div style={{ padding: '16px 16px 12px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            Code Audit
          </div>
          <div style={{ fontSize: 11, color: 'var(--txt2)', marginBottom: 12 }}>
            {selectedRepo?.full_name || 'No repo selected'}
          </div>

          {phase === 'idle' || phase === 'error' ? (
            <button
              onClick={startAudit}
              disabled={!selectedRepo}
              style={{ width: '100%', padding: '9px 0', background: 'var(--accent)', border: 'none', borderRadius: 'var(--radius)', color: 'white', fontSize: 13, fontWeight: 500, cursor: selectedRepo ? 'pointer' : 'not-allowed', opacity: selectedRepo ? 1 : 0.5 }}
            >
              {phase === 'error' ? 'Retry Audit' : '▶  Start Audit'}
            </button>
          ) : phase === 'running' ? (
            <button
              onClick={stopAudit}
              style={{ width: '100%', padding: '9px 0', background: 'transparent', border: '1px solid var(--red)', borderRadius: 'var(--radius)', color: 'var(--red)', fontSize: 13, fontWeight: 500, cursor: 'pointer' }}
            >
              ■  Stop
            </button>
          ) : (
            <>
              <button
                onClick={startAudit}
                style={{ width: '100%', padding: '9px 0', background: 'transparent', border: '1px solid var(--border2)', borderRadius: 'var(--radius)', color: 'var(--txt)', fontSize: 13, cursor: 'pointer', marginBottom: 6 }}
              >
                ↺  Re-run Audit
              </button>
              {report && (
                <button
                  onClick={downloadReport}
                  title="Downloads two PDFs: a short summary with clickable links, and a details file with every finding"
                  style={{ width: '100%', padding: '9px 0', background: 'var(--green-dim)', border: '1px solid var(--green)', borderRadius: 'var(--radius)', color: 'var(--green)', fontSize: 13, fontWeight: 500, cursor: 'pointer' }}
                >
                  ↓ Download Report (2 PDFs)
                </button>
              )}
            </>
          )}
        </div>

        {/* Semgrep autofix → pull request */}
        {report && (
          <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)' }}>
            <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt3)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
              Autofix
            </div>

            {autofixPhase === 'idle' && (
              <button
                onClick={startAutofix}
                title="Runs semgrep --autofix, then pushes a new branch and opens a pull request (forking first if needed) — never commits to the default branch"
                style={{ width: '100%', padding: '9px 0', background: 'var(--accent-dim)', border: '1px solid var(--accent)', borderRadius: 'var(--radius)', color: 'var(--accent)', fontSize: 13, fontWeight: 500, cursor: 'pointer' }}
              >
                ⚡ Autofix with Semgrep → PR
              </button>
            )}

            {autofixPhase === 'running' && (
              <>
                <button
                  onClick={stopAutofix}
                  style={{ width: '100%', padding: '9px 0', background: 'transparent', border: '1px solid var(--red)', borderRadius: 'var(--radius)', color: 'var(--red)', fontSize: 13, fontWeight: 500, cursor: 'pointer', marginBottom: 8 }}
                >
                  ■  Stop
                </button>
                <div style={{ fontSize: 11, fontFamily: 'var(--mono)', lineHeight: 1.6, maxHeight: 120, overflowY: 'auto' }}>
                  {autofixLog.map((entry, i) => (
                    <div key={i} style={{ color: entry.type === 'error' ? 'var(--red)' : entry.type === 'done' ? 'var(--green)' : 'var(--txt2)', marginBottom: 3 }}>
                      <span className="pulse">●</span> {entry.text}
                    </div>
                  ))}
                </div>
              </>
            )}

            {autofixPhase === 'done' && autofixResult && (
              <div>
                <div style={{ fontSize: 12, color: 'var(--green)', marginBottom: 8 }}>
                  ✓ PR opened — {autofixResult.fixes} fix(es) across {autofixResult.files_changed} file(s)
                </div>
                <a
                  href={autofixResult.pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ display: 'block', textAlign: 'center', padding: '9px 0', background: 'var(--green-dim)', border: '1px solid var(--green)', borderRadius: 'var(--radius)', color: 'var(--green)', fontSize: 13, fontWeight: 500, textDecoration: 'none' }}
                >
                  ↗ View Pull Request
                </a>
                <button
                  onClick={() => { setAutofixPhase('idle'); setAutofixResult(null); setAutofixLog([]) }}
                  style={{ width: '100%', padding: '7px 0', marginTop: 6, background: 'transparent', border: '1px solid var(--border2)', borderRadius: 'var(--radius)', color: 'var(--txt2)', fontSize: 12, cursor: 'pointer' }}
                >
                  Run again
                </button>
              </div>
            )}

            {autofixPhase === 'nofix' && (
              <div>
                <div style={{ fontSize: 12, color: 'var(--txt3)', marginBottom: 8 }}>
                  {autofixLog[autofixLog.length - 1]?.text || 'No autofixable findings.'}
                </div>
                <button
                  onClick={startAutofix}
                  style={{ width: '100%', padding: '8px 0', background: 'transparent', border: '1px solid var(--border2)', borderRadius: 'var(--radius)', color: 'var(--txt)', fontSize: 12, cursor: 'pointer' }}
                >
                  ↺  Try again
                </button>
              </div>
            )}

            {autofixPhase === 'error' && (
              <div>
                <div style={{ fontSize: 12, color: 'var(--red)', marginBottom: 8 }}>
                  {autofixLog.find(l => l.type === 'error')?.text || 'Autofix failed.'}
                </div>
                <button
                  onClick={startAutofix}
                  style={{ width: '100%', padding: '8px 0', background: 'transparent', border: '1px solid var(--red)', borderRadius: 'var(--radius)', color: 'var(--red)', fontSize: 12, cursor: 'pointer' }}
                >
                  ↺  Retry
                </button>
              </div>
            )}
          </div>
        )}

        {/* Scanner cards */}
        <div style={{ padding: '10px 12px 0', overflowY: 'auto', flex: 1 }}>
          {/* Pipeline label */}
          <div style={{ fontSize: 10, fontWeight: 500, color: 'var(--txt3)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
            Scanners
          </div>

          {SCANNERS.map((sc, i) => {
            const status = scanStatus[sc.id] || 'pending'
            const count  = scanCounts[sc.id] ?? null
            return (
              <div key={sc.id} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, padding: '7px 8px', borderRadius: 8, background: status === 'running' ? 'var(--accent-dim)' : 'transparent', border: `1px solid ${status === 'running' ? 'var(--accent)' : 'var(--border)'}`, transition: 'all 0.2s' }}>
                {/* Connector line */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0 }}>
                  <div style={{ width: 20, height: 20, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, fontSize: 12,
                    background: status === 'done'    ? 'var(--green-dim)' :
                                status === 'running' ? 'var(--accent-dim)' :
                                status === 'error'   ? 'var(--red-dim)' : 'var(--bg3)',
                    border: `1.5px solid ${
                                status === 'done'    ? 'var(--green)'  :
                                status === 'running' ? 'var(--accent)' :
                                status === 'error'   ? 'var(--red)'    : 'var(--border2)'}`,
                  }}>
                    {status === 'done'    ? '✓' :
                     status === 'running' ? <span className="spin" style={{ fontSize: 10 }}>◌</span> :
                     status === 'error'   ? '✕' :
                     <span style={{ color: 'var(--txt3)', fontSize: 9 }}>{i + 1}</span>}
                  </div>
                </div>

                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500, display: 'flex', alignItems: 'center', gap: 5 }}>
                    <span>{sc.icon}</span>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sc.name}</span>
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--txt3)', marginTop: 1 }}>{sc.tool}</div>
                </div>

                {count !== null && (
                  <span style={{ fontSize: 10, fontWeight: 600, color: count > 0 ? 'var(--yellow)' : 'var(--green)', flexShrink: 0 }}>
                    {count}
                  </span>
                )}
                {status === 'running' && count === null && (
                  <span className="pulse" style={{ fontSize: 10, color: 'var(--accent)' }}>●</span>
                )}
              </div>
            )
          })}

          {/* Live log */}
          <div style={{ fontSize: 10, fontWeight: 500, color: 'var(--txt3)', marginTop: 12, marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.07em' }}>
            Live log
          </div>
          <div style={{ fontSize: 11, fontFamily: 'var(--mono)', lineHeight: 1.6, paddingBottom: 16 }}>
            {log.map((entry, i) => (
              <div key={i} style={{ color: entry.type === 'error' ? 'var(--red)' : entry.type === 'done' ? 'var(--green)' : entry.type === 'warning' ? 'var(--yellow)' : 'var(--txt2)', marginBottom: 3 }}>
                {entry.text}
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>

      {/* ── Right panel: results ──────────────────────────────────────────── */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>

        {/* Idle state */}
        {phase === 'idle' && !report && (
          <div className="fade-in" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 16, color: 'var(--txt3)', textAlign: 'center' }}>
            <div style={{ fontSize: 64 }}>🔬</div>
            <div style={{ fontSize: 18, fontWeight: 600, color: 'var(--txt2)' }}>Python Code Audit</div>
            <div style={{ maxWidth: 420, fontSize: 13, lineHeight: 1.7, color: 'var(--txt3)' }}>
              Runs 7 static analysis tools on <strong style={{ color: 'var(--txt2)' }}>{selectedRepo?.full_name || 'the selected repo'}</strong>:<br/>
              linting · security · CVEs · types · secrets · dead code · patterns
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center', marginTop: 8 }}>
              {SCANNERS.map(sc => (
                <span key={sc.id} style={{ fontSize: 11, padding: '4px 10px', background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 20, color: 'var(--txt2)' }}>
                  {sc.icon} {sc.name}
                </span>
              ))}
            </div>
            <button
              onClick={startAudit}
              disabled={!selectedRepo}
              style={{ marginTop: 8, padding: '10px 28px', background: 'var(--accent)', border: 'none', borderRadius: 'var(--radius)', color: 'white', fontSize: 14, fontWeight: 500, cursor: selectedRepo ? 'pointer' : 'not-allowed', opacity: selectedRepo ? 1 : 0.5 }}
            >
              ▶  Start Audit
            </button>
          </div>
        )}

        {/* Running: progress */}
        {phase === 'running' && !report && (
          <div className="fade-in">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20 }}>
              <span className="pulse" style={{ color: 'var(--accent)', fontSize: 13 }}>● Running scanners…</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
              {SCANNERS.map(sc => {
                const status = scanStatus[sc.id] || 'pending'
                const count  = scanCounts[sc.id] ?? null
                return (
                  <div key={sc.id} style={{ padding: '14px 16px', background: 'var(--bg2)', border: `1px solid ${status === 'running' ? 'var(--accent)' : status === 'done' ? 'var(--green)' : status === 'error' ? 'var(--red)' : 'var(--border)'}`, borderRadius: 'var(--radius)', transition: 'border-color 0.2s' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                      <span style={{ fontSize: 18 }}>{sc.icon}</span>
                      <span style={{ fontSize: 13, fontWeight: 500 }}>{sc.name}</span>
                      {status === 'running' && <span className="spin" style={{ marginLeft: 'auto', color: 'var(--accent)', fontSize: 12 }}>◌</span>}
                      {status === 'done'    && <span style={{ marginLeft: 'auto', color: 'var(--green)', fontSize: 12 }}>✓</span>}
                      {status === 'error'   && <span style={{ marginLeft: 'auto', color: 'var(--red)', fontSize: 12 }}>✕</span>}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--txt3)' }}>{sc.tool}</div>
                    {count !== null && (
                      <div style={{ marginTop: 6, fontSize: 12, color: count > 0 ? 'var(--yellow)' : 'var(--green)', fontWeight: 600 }}>
                        {count} finding{count !== 1 ? 's' : ''}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Done: full report */}
        {report && (
          <div className="fade-in">

            {/* Grade hero ── ── ── ── ── ── ── ── ── ── ── ── */}
            <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'stretch' }}>
              {/* Grade badge */}
              <div style={{ width: 110, flexShrink: 0, background: GRADE_STYLE[report.grade]?.bg, border: `2px solid ${GRADE_STYLE[report.grade]?.border}`, borderRadius: 'var(--radius-lg)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '16px 0' }}>
                <div style={{ fontSize: 48, fontWeight: 700, color: GRADE_STYLE[report.grade]?.color, lineHeight: 1 }}>{report.grade}</div>
                <div style={{ fontSize: 12, color: GRADE_STYLE[report.grade]?.color, marginTop: 4 }}>{report.score}/100</div>
              </div>

              {/* Severity counts */}
              <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8 }}>
                {['critical', 'high', 'medium', 'low', 'info'].map(sev => (
                  <button
                    key={sev}
                    onClick={() => setSevFilter(p => p === sev ? 'all' : sev)}
                    style={{ padding: '10px 6px', background: sevFilter === sev ? SEV[sev].bg : 'var(--bg2)', border: `1px solid ${sevFilter === sev ? SEV[sev].color : 'var(--border)'}`, borderRadius: 'var(--radius)', cursor: 'pointer', textAlign: 'center', transition: 'all 0.15s' }}
                  >
                    <div style={{ fontSize: 22, fontWeight: 700, color: SEV[sev].color }}>{sevCounts[sev] ?? 0}</div>
                    <div style={{ fontSize: 10, color: SEV[sev].color, marginTop: 2, textTransform: 'capitalize' }}>{sev}</div>
                  </button>
                ))}
              </div>

              {/* Meta */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, justifyContent: 'center', minWidth: 80, textAlign: 'center' }}>
                <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--txt)' }}>{totalFindings}</div>
                <div style={{ fontSize: 10, color: 'var(--txt3)' }}>findings</div>
                <div style={{ fontSize: 10, color: 'var(--txt3)' }}>{report.elapsed_s}s</div>
              </div>
            </div>

            {/* Scanner summary bar ── ── ── ── ── ── ── ── ── */}
            <div style={{ display: 'flex', gap: 6, marginBottom: 14, flexWrap: 'wrap' }}>
              <button
                onClick={() => setScanFilter('all')}
                style={{ fontSize: 11, padding: '4px 10px', borderRadius: 20, border: `1px solid ${scanFilter === 'all' ? 'var(--accent)' : 'var(--border)'}`, background: scanFilter === 'all' ? 'var(--accent-dim)' : 'transparent', color: scanFilter === 'all' ? 'var(--accent)' : 'var(--txt2)', cursor: 'pointer' }}
              >
                All scanners
              </button>
              {SCANNERS.map(sc => {
                const cnt = report.by_scanner?.[sc.id] ?? 0
                const active = scanFilter === sc.id
                return (
                  <button
                    key={sc.id}
                    onClick={() => setScanFilter(p => p === sc.id ? 'all' : sc.id)}
                    style={{ fontSize: 11, padding: '4px 10px', borderRadius: 20, border: `1px solid ${active ? 'var(--accent)' : 'var(--border)'}`, background: active ? 'var(--accent-dim)' : 'transparent', color: active ? 'var(--accent)' : 'var(--txt2)', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5 }}
                  >
                    <span>{sc.icon}</span>
                    <span>{sc.name}</span>
                    {cnt > 0 && <span style={{ fontWeight: 600, color: 'var(--yellow)' }}>{cnt}</span>}
                  </button>
                )
              })}
            </div>

            {/* Tabs ── ── ── ── ── ── ── ── ── ── ── ── ── ── */}
            <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid var(--border)', marginBottom: 12 }}>
              {['findings', 'overview'].map(tab => (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  style={{ padding: '7px 14px', border: 'none', borderBottom: activeTab === tab ? '2px solid var(--accent)' : '2px solid transparent', background: 'transparent', color: activeTab === tab ? 'var(--txt)' : 'var(--txt3)', fontSize: 12, fontWeight: activeTab === tab ? 500 : 400, cursor: 'pointer', textTransform: 'capitalize' }}
                >
                  {tab}
                </button>
              ))}
              <div style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--txt3)', alignSelf: 'center', paddingRight: 4 }}>
                {filtered.length} / {totalFindings} shown
              </div>
            </div>

            {/* Findings table ── ── ── ── ── ── ── ── ── ── ── */}
            {activeTab === 'findings' && (
              <div>
                {filtered.length === 0 ? (
                  <div style={{ padding: '40px 0', textAlign: 'center', color: 'var(--txt3)', fontSize: 13 }}>
                    {totalFindings === 0 ? '🎉 No findings — clean repo!' : 'No findings match the current filter.'}
                  </div>
                ) : (
                  filtered.map((f, i) => {
                    const s = SEV[f.severity] || SEV.info
                    const isOpen = expandedFinding === i
                    return (
                      <div
                        key={i}
                        onClick={() => setExpandedFinding(isOpen ? null : i)}
                        style={{ marginBottom: 5, padding: '9px 12px', background: 'var(--bg2)', border: `1px solid ${isOpen ? s.color : 'var(--border)'}`, borderRadius: 'var(--radius)', cursor: 'pointer', transition: 'border-color 0.15s' }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          {/* Severity pill */}
                          <span style={{ fontSize: 10, padding: '2px 7px', borderRadius: 20, background: s.bg, color: s.color, fontWeight: 600, flexShrink: 0, minWidth: 56, textAlign: 'center', textTransform: 'capitalize' }}>
                            {f.severity}
                          </span>
                          {/* Scanner badge */}
                          <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0, width: 80, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {SCANNERS.find(sc => sc.id === f.scanner)?.icon} {f.scanner}
                          </span>
                          {/* Rule */}
                          {f.rule && (
                            <code style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--accent)', flexShrink: 0 }}>{f.rule}</code>
                          )}
                          {/* Message preview */}
                          <span style={{ fontSize: 12, color: 'var(--txt)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {f.message}
                          </span>
                          {/* Location */}
                          {f.file && (
                            <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {f.file}{f.line ? `:${f.line}` : ''}
                            </span>
                          )}
                          <span style={{ fontSize: 10, color: 'var(--txt3)', flexShrink: 0 }}>{isOpen ? '▲' : '▼'}</span>
                        </div>

                        {/* Expanded detail */}
                        {isOpen && (
                          <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
                            <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr', gap: '6px 12px', fontSize: 12 }}>
                              {f.file && <><span style={{ color: 'var(--txt3)' }}>File</span><code style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--txt2)' }}>{f.file}{f.line ? `:${f.line}` : ''}</code></>}
                              {f.rule && <><span style={{ color: 'var(--txt3)' }}>Rule</span><code style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--accent)' }}>{f.rule}</code></>}
                              {f.confidence && <><span style={{ color: 'var(--txt3)' }}>Confidence</span><span style={{ color: 'var(--txt2)' }}>{f.confidence}</span></>}
                              <span style={{ color: 'var(--txt3)' }}>Message</span>
                              <span style={{ color: 'var(--txt)', lineHeight: 1.5 }}>{f.message}</span>
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })
                )}
              </div>
            )}

            {/* Overview tab ── ── ── ── ── ── ── ── ── ── ── */}
            {activeTab === 'overview' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                {/* Score breakdown */}
                <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '14px 18px' }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Score breakdown</div>
                  {['critical','high','medium','low','info'].map(sev => {
                    const cnt = sevCounts[sev] || 0
                    const exposure = CVSS_WEIGHTS[sev] * cnt
                    return (
                      <div key={sev} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                        <span style={{ fontSize: 11, color: SEV[sev].color, width: 56, textTransform: 'capitalize' }}>{sev}</span>
                        <div style={{ flex: 1, height: 6, background: 'var(--bg3)', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${Math.min(100, cnt * 5)}%`, background: SEV[sev].color, borderRadius: 3 }} />
                        </div>
                        <span style={{ fontSize: 11, color: 'var(--txt2)', width: 28, textAlign: 'right' }}>{cnt}</span>
                        {exposure > 0 && <span style={{ fontSize: 10, color: 'var(--txt3)', width: 76, textAlign: 'right' }}>{CVSS_WEIGHTS[sev].toFixed(1)} × {cnt} CVSS</span>}
                      </div>
                    )
                  })}
                  <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)', fontSize: 12, display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: 'var(--txt2)' }}>Final score</span>
                    <span style={{ fontWeight: 700, color: GRADE_STYLE[report.grade]?.color }}>{report.score}/100 (Grade {report.grade})</span>
                  </div>
                  <div style={{ marginTop: 6, fontSize: 9.5, color: 'var(--txt3)', lineHeight: 1.5 }}>
                    Score = 100 × e <sup style={{ fontSize: 8 }}>−exposure/40</sup> — exposure is the CVSS-weighted sum of all findings (CVSS v3.1 severity bands). Diminishing marginal penalty per finding, no hard floor at zero.
                  </div>
                </div>

                {/* Per-scanner counts */}
                <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '14px 18px' }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Findings per scanner</div>
                  {SCANNERS.map(sc => {
                    const cnt = report.by_scanner?.[sc.id] ?? 0
                    const max = Math.max(...SCANNERS.map(s => report.by_scanner?.[s.id] ?? 0), 1)
                    return (
                      <div key={sc.id} style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                        <span style={{ fontSize: 14, width: 20 }}>{sc.icon}</span>
                        <span style={{ fontSize: 11, color: 'var(--txt2)', width: 110, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sc.name}</span>
                        <div style={{ flex: 1, height: 6, background: 'var(--bg3)', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${(cnt / max) * 100}%`, background: cnt > 0 ? 'var(--accent)' : 'var(--bg3)', borderRadius: 3 }} />
                        </div>
                        <span style={{ fontSize: 11, color: cnt > 0 ? 'var(--yellow)' : 'var(--green)', width: 24, textAlign: 'right' }}>{cnt}</span>
                      </div>
                    )
                  })}
                </div>

                {/* Pipeline summary */}
                <div style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '14px 18px', gridColumn: '1/-1' }}>
                  <div style={{ fontSize: 11, fontWeight: 500, color: 'var(--txt2)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>Audit pipeline</div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 0, flexWrap: 'wrap' }}>
                    {[
                      { label: 'GitHub Repo', icon: '🔗' },
                      { label: 'Clone / Load', icon: '📥' },
                      ...SCANNERS.map(sc => ({ label: sc.name, icon: sc.icon, id: sc.id })),
                      { label: 'Normalize', icon: '⚙️' },
                      { label: 'Score', icon: '📊' },
                      { label: 'Audit Report', icon: '📋' },
                    ].map((node, i, arr) => (
                      <div key={i} style={{ display: 'flex', alignItems: 'center' }}>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                          <div style={{ width: 36, height: 36, borderRadius: 8, background: node.id ? (scanStatus[node.id] === 'done' ? 'var(--green-dim)' : 'var(--bg3)') : 'var(--bg3)', border: `1px solid ${node.id && scanStatus[node.id] === 'done' ? 'var(--green)' : 'var(--border)'}`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16 }}>
                            {node.icon}
                          </div>
                          <span style={{ fontSize: 9, color: 'var(--txt3)', textAlign: 'center', maxWidth: 48, lineHeight: 1.3 }}>{node.label}</span>
                        </div>
                        {i < arr.length - 1 && (
                          <div style={{ width: 16, height: 1, background: 'var(--border2)', margin: '0 2px', marginBottom: 14 }} />
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

          </div>
        )}
      </div>
    </div>
  )
}
