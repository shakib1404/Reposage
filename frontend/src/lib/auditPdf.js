/**
 * auditPdf.js — the audit report document.
 *
 * One PDF, not two. The previous version shipped a terse two-page summary plus
 * a separate appendix and linked between them with `file.pdf#page=N` URLs, which
 * only resolve while both files sit in the same folder and the viewer honours
 * the fragment — in practice they resolved for nobody. A single document links
 * internally with real PDF destinations, so every "view" jumps where it says.
 *
 * Structure: cover, contents, then eight numbered sections and an appendix that
 * lists every occurrence of every finding. The report is meant to be readable
 * without the app open next to it, so each section states what it is showing
 * and what the reader should do about it.
 */
import { jsPDF } from 'jspdf'
import { SCANNERS, SC, RULE_INFO } from './auditRules'

// ── Geometry (mm, A4) ────────────────────────────────────────────────────────
const PW = 210, PH = 297
const M  = 16                       // page margin
const CW = PW - M * 2               // content width
const FOOT = 14                     // reserved footer band

// ── Palette ──────────────────────────────────────────────────────────────────
const INK    = [21, 22, 26]
const BODY   = [68, 70, 79]
const MUTED  = [138, 141, 153]
const FAINT  = [226, 227, 232]
const WASH   = [247, 247, 250]
const ACCENT = [79, 107, 237]
const PAPER  = [255, 255, 255]

const SEV_RGB = {
  critical: [220,  68,  68],
  high:     [226, 123,  36],
  medium:   [201, 155,  18],
  low:      [ 34, 160, 118],
  info:     [128, 132, 146],
}
const GRADE_RGB = {
  A: [ 34, 160, 118], B: [ 61, 118, 220], C: [201, 155,  18],
  D: [226, 123,  36], F: [220,  68,  68],
}
const SEV_ORDER = ['critical', 'high', 'medium', 'low', 'info']
// How many occurrences of one issue type get their source printed in the
// appendix. Three is enough to see whether they are the same mistake repeated
// or genuinely different sites.
const APPENDIX_CODE_LIMIT = 3
const SEV_RANK  = { critical: 0, high: 1, medium: 2, low: 3, info: 4 }

// Must match backend tester.py CVSS_WEIGHTS — the midpoint of each CVSS v3.1
// qualitative band. Shown in the report so the score is reproducible by hand.
const CVSS_WEIGHTS = { critical: 9.5, high: 8.0, medium: 5.5, low: 2.0, info: 0.0 }
const DECAY_K = 220.0
const MIN_LOC = 300

const GRADE_BANDS = [
  ['A', '90 - 100', 'Very few weighted findings per thousand lines. Nothing here needs scheduling.'],
  ['B', '75 - 89',  'Healthy. A handful of real issues worth folding into normal work.'],
  ['C', '60 - 74',  'Noticeable defect density. Worth a dedicated pass before the next release.'],
  ['D', '40 - 59',  'High density, or a few severe findings. Address the critical and high rows first.'],
  ['F', '0 - 39',   'Either many findings or several severe ones. Treat as a blocker for shipping.'],
]

const SEVERITY_INFO = {
  critical: ['Exploitable or data-losing, with no mitigating condition. Fix before release.',
             'CVSS 9.0 - 10.0'],
  high:     ['A real defect on a reachable path. Schedule it deliberately, not opportunistically.',
             'CVSS 7.0 - 8.9'],
  medium:   ['Wrong under some inputs, or a maintainability problem that will cause a defect later.',
             'CVSS 4.0 - 6.9'],
  low:      ['Minor correctness or style issue. Safe to batch.', 'CVSS 0.1 - 3.9'],
  info:     ['Observation, not a defect. Carries zero weight in the score.', 'CVSS 0.0'],
}

const AREA_INFO = {
  source:  'Shipped code. The grade is computed from these findings only.',
  test:    'Test suite. Reported in full, excluded from the grade.',
  docs:    'Documentation and doc examples. Reported, not graded.',
  example: 'Example and sample scripts. Reported, not graded.',
}

// ─────────────────────────────────────────────────────────────────────────────
// Text encoding
// ─────────────────────────────────────────────────────────────────────────────
/**
 * jsPDF's built-in fonts are WinAnsi-only, and characters outside that
 * repertoire do not fail loudly — they render as mojibake. Verified in the
 * previous report: the architecture findings' module arrows came out as `!'`
 * and "2 sigma above the mean" as `2Ã`, silently, in every PDF produced.
 * Backend messages are written for the browser, so they are translated here
 * rather than being degraded at the source.
 */
const CHAR_MAP = {
  '→': '->', '←': '<-', '⇒': '=>',
  'σ': 'sigma', 'μ': 'mu', 'Δ': 'delta',
  '≥': '>=', '≤': '<=', '≠': '!=', '≈': '~',
  '✓': 'v', '✔': 'v', '✗': 'x', '✘': 'x',
  '•': '·', '●': '·', '▪': '·',
  '‘': "'", '’': "'", '“': '"', '”': '"',
  '′': "'", ' ': ' ', '​': '', '️': '',
}
function enc(s) {
  let out = ''
  for (const ch of String(s ?? '')) {
    if (ch in CHAR_MAP) { out += CHAR_MAP[ch]; continue }
    const c = ch.codePointAt(0)
    // Keep Latin-1 plus the handful of WinAnsi upper slots we rely on
    // (em dash, ellipsis, quotes); drop anything else, emoji included, rather
    // than emitting a byte the font will draw as a different glyph.
    if (c < 0x100 || ch === '—' || ch === '–' || ch === '…') out += ch
  }
  return out
}

/** Mix a colour towards white. jsPDF's alpha support is patchy across viewers,
 *  so highlight bands are pre-mixed opaque colours instead. */
function tint(rgb, amount = 0.88) {
  return rgb.map(c => Math.round(c + (255 - c) * amount))
}

// ─────────────────────────────────────────────────────────────────────────────
// Layout engine
// ─────────────────────────────────────────────────────────────────────────────
function layout(doc) {
  const st = { y: 0, repo: '', toc: [], marks: [] }

  const set = (size, weight = 'normal', color = BODY) => {
    doc.setFontSize(size)
    doc.setFont('helvetica', weight)
    doc.setTextColor(...color)
  }

  const api = {
    get y() { return st.y },
    set y(v) { st.y = v },
    get page() { return doc.internal.getCurrentPageInfo().pageNumber },
    st, doc, set,

    /** Start a fresh content page. The running header is NOT drawn here: at
     *  page-break time the section that will occupy the page is often not the
     *  one that just ended, which put "1. Executive summary" at the top of the
     *  page holding sections 2 and 3. Headers are stamped at the end instead,
     *  from the marks each section leaves behind. */
    newPage() {
      doc.addPage()
      st.y = M + 8
      return api
    },

    /** Break only if `h` mm will not fit above the footer band. */
    need(h) {
      if (st.y + h > PH - FOOT) api.newPage()
      return api
    },

    /** Record which section owns this page onward. */
    mark(label) { st.marks.push({ page: api.page, label }); return api },

    rule(gap = 0, color = FAINT) {
      st.y += gap
      doc.setDrawColor(...color)
      doc.setLineWidth(0.2)
      doc.line(M, st.y, PW - M, st.y)
      return api
    },

    /** Numbered section opener. Records the page for the contents list. */
    section(num, title, standfirst) {
      // A section needs room to be worth starting. Without this the opener and
      // standfirst land at the foot of a page and the body begins on the next,
      // which reads as a mistake.
      api.need(72)
      if (st.y > M + 10) { st.y += 7 }
      api.mark(`${num}. ${title}`)
      st.toc.push({ num, title, page: api.page })

      set(7.6, 'bold', ACCENT)
      doc.text(enc(`SECTION ${num}`), M, st.y)
      st.y += 7
      set(18, 'bold', INK)
      doc.text(enc(title), M, st.y)
      st.y += 3
      doc.setDrawColor(...ACCENT)
      doc.setLineWidth(0.8)
      doc.line(M, st.y, M + 14, st.y)
      st.y += 6
      if (standfirst) api.para(standfirst, { size: 9, color: BODY, lead: 4.6 })
      st.y += 2
      return api
    },

    h2(title) {
      api.need(16)
      st.y += 3
      set(11.5, 'bold', INK)
      doc.text(enc(title), M, st.y)
      st.y += 6
      return api
    },

    label(text) {
      api.need(10)
      set(7.2, 'bold', [108, 111, 124])
      doc.text(enc(text.toUpperCase()), M, st.y)
      st.y += 4.6
      return api
    },

    /** Wrapped paragraph. Breaks across pages line by line. */
    para(text, { size = 8.4, color = BODY, lead = 4.2, x = M, w = CW,
                 weight = 'normal', gap = 3.4 } = {}) {
      set(size, weight, color)
      for (const line of doc.splitTextToSize(enc(text), w)) {
        api.need(lead + 1)
        doc.text(line, x, st.y)
        st.y += lead
      }
      st.y += gap
      return api
    },

    /** Single line, no wrapping. */
    line(text, { size = 8.4, color = BODY, x = M, weight = 'normal',
                 align, advance = 4.6 } = {}) {
      api.need(advance + 1)
      set(size, weight, color)
      doc.text(enc(text), x, st.y, align ? { align } : undefined)
      st.y += advance
      return api
    },

    /** Horizontal meter. */
    bar(x, y, w, h, frac, color = ACCENT, track = [235, 236, 240]) {
      // `track: null` draws the fill only, for overlaying one bar on another.
      if (track) {
        doc.setFillColor(...track)
        doc.roundedRect(x, y, w, h, h / 2, h / 2, 'F')
      }
      const fw = Math.max(0, Math.min(1, frac)) * w
      if (fw > 0.3) {
        doc.setFillColor(...color)
        doc.roundedRect(x, y, Math.max(fw, h), h, h / 2, h / 2, 'F')
      }
      return api
    },

    /**
     * The source lines a finding is about, with a line-number gutter and the
     * offending line called out. Courier, because it is the only monospace
     * face jsPDF's built-in set offers and column alignment is the whole point.
     */
    code(lines, startLine, hitLine, rgb = SEV_RGB.info, { x = M, w = CW } = {}) {
      if (!lines || !lines.length) return api
      const LH = 3.5, PAD = 1.6
      const last = startLine + lines.length - 1
      const gutterW = Math.max(6, String(last).length * 1.7 + 3)

      api.need(lines.length * LH + PAD * 2 + 2)
      const top = st.y - 2.6

      doc.setFillColor(247, 248, 250)
      doc.setDrawColor(...FAINT)
      doc.setLineWidth(0.2)
      doc.roundedRect(x, top, w, lines.length * LH + PAD * 2, 1, 1, 'FD')
      doc.setFillColor(238, 239, 243)
      doc.rect(x + 0.4, top + 0.4, gutterW, lines.length * LH + PAD * 2 - 0.8, 'F')

      let y = top + PAD + 2.5
      lines.forEach((text, i) => {
        const n = startLine + i
        const hit = n === hitLine
        if (hit) {
          doc.setFillColor(...tint(rgb))
          doc.rect(x + gutterW + 0.4, y - 2.6, w - gutterW - 0.8, LH, 'F')
          doc.setFillColor(...rgb)
          doc.rect(x + gutterW + 0.4, y - 2.6, 0.8, LH, 'F')
        }
        doc.setFont('courier', hit ? 'bold' : 'normal')
        doc.setFontSize(6.2)
        doc.setTextColor(...(hit ? rgb : MUTED))
        doc.text(String(n), x + gutterW - 1.5, y, { align: 'right' })
        doc.setFontSize(6.6)
        doc.setTextColor(...(hit ? [20, 21, 25] : [92, 95, 106]))
        // Clipping rather than wrapping: a wrapped line destroys the
        // correspondence between a row and its line number, which is the only
        // reason this block exists.
        doc.text(fitMono(doc, text, w - gutterW - 4, 6.6),
                 x + gutterW + 2, y)
        y += LH
      })
      doc.setFont('helvetica', 'normal')
      st.y = top + lines.length * LH + PAD * 2 + 3
      return api
    },

    /** Filled severity chip. Returns its width. */
    pill(x, y, text, rgb, { size = 6.4, padX = 2 } = {}) {
      set(size, 'bold', PAPER)
      const w = doc.getTextWidth(enc(text)) + padX * 2
      doc.setFillColor(...rgb)
      doc.roundedRect(x, y - 3.1, w, 4.4, 1, 1, 'F')
      doc.text(enc(text), x + padX, y)
      return w
    },
  }
  return api
}

// ─────────────────────────────────────────────────────────────────────────────
// Report data shaping
// ─────────────────────────────────────────────────────────────────────────────
function groupFindings(findings) {
  const map = new Map()
  for (const f of findings) {
    const key = `${f.scanner}::${f.rule || f.message}`
    let g = map.get(key)
    if (!g) {
      g = { scanner: f.scanner, rule: f.rule, severity: f.severity,
            message: f.message, area: f.area || 'source', items: [] }
      map.set(key, g)
    }
    g.items.push(f)
    if (SEV_RANK[f.severity] < SEV_RANK[g.severity]) g.severity = f.severity
  }
  // An issue type is rarely confined to one part of the repo - "line too long"
  // lands in src/ and tests/ alike - so the group cannot just inherit the area
  // of whichever finding happened to arrive first, which labelled mixed groups
  // "test, not graded" while half their occurrences were graded.
  for (const g of map.values()) {
    g.graded = g.items.filter(f => (f.area || 'source') === 'source').length
    const others = [...new Set(g.items.filter(f => (f.area || 'source') !== 'source')
                                      .map(f => f.area))]
    g.areaLabel =
      g.graded === g.items.length ? ''
      : g.graded === 0            ? `${others.join('/')}, not graded`
      : `${g.items.length - g.graded} of ${g.items.length} not graded`
    // The index is a fixed-column table with ~15mm for this; the long form
    // truncated to "test, not gra...". The appendix header has the room.
    g.areaShort =
      g.graded === g.items.length ? ''
      : g.graded === 0            ? 'not graded'
      : `${g.items.length - g.graded} ungraded`
  }
  return [...map.values()].sort((a, b) =>
    (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9) ||
    b.items.length - a.items.length)
}

function pct(n, d) { return d > 0 ? Math.round((n / d) * 100) : 0 }

/** Clip `text` to `w` mm at the current font, with an ellipsis if it was cut.
 *  The index is a fixed-column table and rule identifiers run from "E501" to
 *  "python.lang.security.audit.dangerous-subprocess-use"; without this the long
 *  ones ran straight across the message column. */
/** Same idea as fit(), but measured in the Courier face the code block uses. */
function fitMono(doc, text, w, size) {
  const prevSize = doc.getFontSize()
  doc.setFont('courier', 'normal')
  doc.setFontSize(size)
  const t = enc(String(text ?? '').replace(/\t/g, '    '))
  const out = doc.getTextWidth(t) <= w ? t : (() => {
    let lo = 0, hi = t.length
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (doc.getTextWidth(t.slice(0, mid) + '...') <= w) lo = mid
      else hi = mid - 1
    }
    return t.slice(0, lo) + '...'
  })()
  doc.setFontSize(prevSize)
  return out
}

function fit(doc, text, w) {
  const t = enc(text || '')
  if (doc.getTextWidth(t) <= w) return t
  let lo = 0, hi = t.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (doc.getTextWidth(t.slice(0, mid) + '...') <= w) lo = mid
    else hi = mid - 1
  }
  return t.slice(0, lo) + '...'
}

/** The executive summary, composed from the numbers rather than templated. */
function prose(report, groups) {
  const sev   = report.severity || {}
  const all   = report.severity_all || sev
  const total = report.total || 0
  const graded = report.graded_on ?? total
  const nonSrc = total - graded
  const loc   = report.source_loc || 0
  const bySc  = report.by_scanner || {}
  const ran   = SCANNERS.filter(s => (bySc[s.id] ?? null) !== null).length || SCANNERS.length
  const out = []

  out.push(
    `RepoSage audited ${report.repo} with ${SCANNERS.length} scanners in ` +
    `${report.elapsed_s || 0} seconds. It reports ${total} finding` +
    `${total === 1 ? '' : 's'} across ${groups.length} distinct issue type` +
    `${groups.length === 1 ? '' : 's'}` +
    (loc ? `, measured against ${loc.toLocaleString()} lines of source code` : '') + '.')

  const band = GRADE_BANDS.find(b => b[0] === report.grade)
  out.push(
    `The repository scores ${report.score} out of 100, grade ${report.grade}. ` +
    (band ? band[2] : '') +
    ` The score is CVSS-weighted finding density per thousand lines of source, ` +
    `not a raw count, so it is comparable between a small project and a large one.`)

  const sevBits = SEV_ORDER.filter(k => (sev[k] || 0) > 0)
    .map(k => `${sev[k]} ${k}`)
  if ((sev.critical || 0) + (sev.high || 0) > 0) {
    out.push(
      `In shipped code there ${sev.critical === 1 && !sev.high ? 'is' : 'are'} ` +
      `${sev.critical || 0} critical and ${sev.high || 0} high-severity ` +
      `finding${(sev.critical || 0) + (sev.high || 0) === 1 ? '' : 's'}; these ` +
      `carry most of the weight in the score and should be read first. The full ` +
      `graded profile is ${sevBits.join(', ')}.`)
  } else if (graded > 0) {
    out.push(
      `No critical or high-severity findings were reported in shipped code. The ` +
      `graded profile is ${sevBits.join(', ') || 'empty'}, so what remains is ` +
      `maintenance work rather than anything urgent.`)
  } else {
    out.push(`No findings were reported in shipped code.`)
  }

  const top = Object.entries(bySc).filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])[0]
  if (top) {
    out.push(
      `${SC[top[0]]?.name || top[0]} accounts for the largest share at ` +
      `${top[1]} finding${top[1] === 1 ? '' : 's'} (${pct(top[1], total)}% of the ` +
      `total). Section 5 breaks every scanner down and says what each one looks for.`)
  }

  if (nonSrc > 0) {
    out.push(
      `${nonSrc} of the ${total} findings (${pct(nonSrc, total)}%) are in tests, ` +
      `documentation or examples. They are listed in full but excluded from the ` +
      `grade: a test file is supposed to contain assertions, and counting those ` +
      `as defects is what used to grade well-maintained libraries an F.`)
  }

  const ownN = (bySc.arch || 0) + (bySc.excflow || 0)
  if (ownN > 0) {
    out.push(
      `${ownN} finding${ownN === 1 ? '' : 's'} came from RepoSage's own two ` +
      `analyses, which read the repository as a graph rather than one file at a ` +
      `time. No wrapped linter reports any of them. Section 8 explains how they work.`)
  }
  return out
}

// ─────────────────────────────────────────────────────────────────────────────
// Entry point
// ─────────────────────────────────────────────────────────────────────────────
/** Build the document. Split out from the download so it can be rendered and
 *  inspected outside a browser. */
export function buildAuditReport(report) {
  const doc = new jsPDF({ unit: 'mm', format: 'a4', compress: true })
  const L = layout(doc)
  L.st.repo = report.repo || 'repository'

  const findings = report.findings || []
  const groups   = groupFindings(findings)

  cover(L, report, groups)
  const tocPage = reserveContents(L)

  execSummary(L, report, groups)
  howToRead(L, report)
  scoring(L, report)
  severityProfile(L, report)
  whereFindings(L, report)
  scannerCoverage(L, report)
  priorityFindings(L, report, groups)
  const indexLinks = issueIndex(L, report, groups)
  ownAnalysis(L, report)
  appendix(L, report, groups, indexLinks[0]?.page)

  resolveIndexLinks(doc, indexLinks)
  drawContents(L, tocPage)
  stampFooters(doc, report, L.st.marks)
  return doc
}

export function downloadAuditReport(report) {
  if (!report) return
  const slug = (report.repo || 'report').replace(/[/\\]/g, '_')
  buildAuditReport(report).save(`audit_report_${slug}.pdf`)
}

// ─────────────────────────────────────────────────────────────────────────────
// Cover
// ─────────────────────────────────────────────────────────────────────────────
/**
 * A title page, not a dashboard tile. The previous cover packed a full-width
 * 100mm band, a cornered grade box and four side-by-side tiles into the top
 * third of the sheet — a composition built for a landscape slide, stretched
 * over a portrait page. Every number it showed is also in the body (sections
 * 3-7), so the fix is to let the cover be what a professional report's first
 * page actually is: title, one strong vertical focal point, and a close.
 */
function cover(L, report, groups) {
  const { doc } = L
  const g = report.grade || 'F'
  const gradeRgb = GRADE_RGB[g] || MUTED
  const band = GRADE_BANDS.find(b => b[0] === g)

  // A hairline, not a hero band — the only colour at the very top of the page.
  doc.setFillColor(...ACCENT)
  doc.rect(0, 0, PW, 2.2, 'F')

  let y = 44
  L.set(8.4, 'bold', ACCENT)
  doc.text(enc('C O D E   A U D I T   R E P O R T'), M, y)
  y += 5
  doc.setDrawColor(...ACCENT)
  doc.setLineWidth(0.8)
  doc.line(M, y, M + 16, y)
  y += 15

  L.set(28, 'bold', INK)
  const titleLines = doc.splitTextToSize(enc(report.repo || 'repository'), CW).slice(0, 3)
  for (const ln of titleLines) { doc.text(ln, M, y); y += 11 }
  y += 3

  L.set(9.2, 'normal', MUTED)
  doc.text(enc(
    `Static analysis across ${SCANNERS.length} scanners  ·  ` +
    `${(report.findings || []).length} findings  ·  ${groups.length} issue types`
  ), M, y)
  y += 6
  L.set(7.6, 'normal', [152, 155, 168])
  doc.text(enc(`Generated ${new Date().toLocaleString()}`), M, y)
  y += 9

  doc.setDrawColor(...FAINT)
  doc.setLineWidth(0.2)
  doc.line(M, y, PW - M, y)

  // ── The seal: the single focal point, centred, reading top to bottom ──────
  const cy = 158, r = 26
  doc.setFillColor(...tint(gradeRgb, 0.9))
  doc.circle(PW / 2, cy, r + 4, 'F')
  doc.setFillColor(...gradeRgb)
  doc.circle(PW / 2, cy, r, 'F')
  L.set(32, 'bold', PAPER)
  doc.text(enc(g), PW / 2, cy + 6, { align: 'center' })

  L.set(10.5, 'bold', INK)
  doc.text(enc(`${report.score} / 100`), PW / 2, cy + r + 12, { align: 'center' })
  if (band) {
    L.set(8, 'normal', MUTED)
    let by_ = cy + r + 19
    for (const ln of doc.splitTextToSize(enc(band[2]), 112)) {
      doc.text(ln, PW / 2, by_, { align: 'center' }); by_ += 4.2
    }
  }

  // ── Headline numbers, as a quiet caption row — not boxed tiles ────────────
  const statsY = 233
  const stats = [
    [String((report.findings || []).length), 'TOTAL FINDINGS'],
    [String(report.graded_on ?? (report.findings || []).length), 'GRADED (SOURCE)'],
    [(report.source_loc || 0).toLocaleString(), 'LINES OF SOURCE'],
    [`${report.elapsed_s || 0}s`, 'SCAN TIME'],
  ]
  const sw = CW / stats.length
  doc.setDrawColor(...FAINT)
  doc.setLineWidth(0.2)
  stats.forEach(([v, k], i) => {
    const x = M + i * sw + sw / 2
    L.set(13, 'bold', INK)
    doc.text(enc(v), x, statsY, { align: 'center' })
    L.set(6, 'normal', MUTED)
    doc.text(enc(k), x, statsY + 4.6, { align: 'center' })
    if (i > 0) doc.line(M + i * sw, statsY - 7.5, M + i * sw, statsY + 5.4)
  })

  // ── Colophon ──────────────────────────────────────────────────────────────
  // Pinned high enough that its last wrapped line still clears the footer band:
  // one line of overflow here spills the cover onto a second page and pushes a
  // blank sheet in front of the contents.
  L.y = PH - 36
  L.rule(0)
  L.y += 5
  L.line('RepoSage', { size: 9, weight: 'bold', color: INK, advance: 4.4 })
  L.para(
    'Automated static analysis. Every finding in this report was produced by a ' +
    'tool, not by a reviewer, and a static analyser cannot see intent. Section 2 ' +
    'explains what the grade does and does not mean.',
    { size: 7, color: MUTED, lead: 3.4, gap: 0 })
}

// ─────────────────────────────────────────────────────────────────────────────
// Contents — laid out last, so it needs its page reserved now
// ─────────────────────────────────────────────────────────────────────────────
function reserveContents(L) {
  L.doc.addPage()
  return L.page
}

function drawContents(L, pageNo) {
  const { doc } = L
  const back = L.page
  doc.setPage(pageNo)

  let y = M + 10
  L.set(7.2, 'bold', ACCENT)
  doc.text('CONTENTS', M, y)
  y += 9
  L.set(16, 'bold', INK)
  doc.text("What is in this report", M, y)
  y += 4
  doc.setDrawColor(...ACCENT)
  doc.setLineWidth(0.8)
  doc.line(M, y, M + 14, y)
  y += 11

  for (const t of L.st.toc) {
    L.set(8.2, 'bold', ACCENT)
    doc.text(enc(String(t.num)), M, y)
    L.set(9, 'normal', INK)
    doc.text(enc(t.title), M + 8, y)
    // Leader dots, drawn to stop short of the page number
    const tx = M + 8 + doc.getTextWidth(enc(t.title)) + 2
    const px = PW - M - 8
    L.set(9, 'normal', [214, 215, 222])
    let dx = tx
    let dots = ''
    while (dx < px) { dots += '.'; dx += doc.getTextWidth('.') }
    doc.text(dots, tx, y)
    L.set(9, 'bold', INK)
    doc.text(enc(String(t.page)), PW - M, y, { align: 'right' })
    doc.link(M, y - 4, CW, 6, { pageNumber: t.page })
    y += 8.4
  }

  y += 4
  doc.setDrawColor(...FAINT)
  doc.setLineWidth(0.2)
  doc.line(M, y, PW - M, y)
  y += 6
  L.set(7, 'normal', MUTED)
  for (const ln of doc.splitTextToSize(enc(
    'Every row above is a link, as is every "view" in the issue index. ' +
    'The appendix lists each finding individually with its file and line.'), CW)) {
    doc.text(ln, M, y); y += 3.6
  }

  doc.setPage(back)
}

// ─────────────────────────────────────────────────────────────────────────────
// 1. Executive summary
// ─────────────────────────────────────────────────────────────────────────────
function execSummary(L, report, groups) {
  L.newPage()
  L.section(1, 'Executive summary',
    'What the audit found, in the order a reader needs it.')
  for (const p of prose(report, groups)) {
    L.para(p, { size: 8.8, lead: 4.6 })
  }

  // The three worst issue types, named, so the summary is actionable on its own.
  const top = groups.filter(g => g.graded > 0).slice(0, 3)
  if (top.length) {
    L.h2('Start here')
    top.forEach((g, i) => {
      L.need(18)
      L.set(8, 'bold', INK)
      L.doc.text(enc(`${i + 1}.`), M, L.y)
      const pw = L.pill(M + 6, L.y, g.severity.toUpperCase(), SEV_RGB[g.severity] || MUTED)
      L.set(8, 'bold', INK)
      L.doc.text(enc(`${SC[g.scanner]?.name || g.scanner}${g.rule ? '  ' + g.rule : ''}`),
                 M + 8 + pw, L.y)
      L.set(7.6, 'normal', MUTED)
      L.doc.text(enc(`${g.items.length} occurrence${g.items.length === 1 ? '' : 's'}`),
                 PW - M, L.y, { align: 'right' })
      L.y += 4.4
      L.para(g.message, { size: 7.8, lead: 3.8, x: M + 6, w: CW - 6, gap: 3.6 })
    })
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. How to read this report
// ─────────────────────────────────────────────────────────────────────────────
function howToRead(L, report) {
  const { doc } = L
  L.section(2, 'How to read this report',
    'Three things decide what a finding means here: how severe it is, where in ' +
    'the repository it lives, and whether it counts toward the grade. All three ' +
    'are defined below so nothing in the rest of the report has to be guessed at.')

  L.h2('Grades')
  L.para(
    'The grade is a band on the score, and the score is a density. A large, ' +
    'mature project and a single-file script are therefore directly comparable, ' +
    'which an absolute finding count never is.', { size: 8, lead: 4 })
  for (const [g, band, meaning] of GRADE_BANDS) {
    L.need(12)
    const y0 = L.y
    const mine = g === report.grade
    if (mine) {
      doc.setFillColor(...WASH)
      doc.rect(M - 2, y0 - 4.6, CW + 4, 11, 'F')
    }
    doc.setFillColor(...(GRADE_RGB[g] || MUTED))
    doc.roundedRect(M, y0 - 4, 7, 7, 1, 1, 'F')
    L.set(7.6, 'bold', PAPER)
    doc.text(enc(g), M + 3.5, y0 + 0.9, { align: 'center' })
    L.set(7.6, 'bold', INK)
    doc.text(enc(band), M + 10, y0 + 0.6)
    L.set(7.6, 'normal', BODY)
    doc.text(enc(meaning), M + 28, y0 + 0.6)
    if (mine) {
      L.set(6.6, 'bold', ACCENT)
      doc.text('THIS REPO', PW - M, y0 + 0.6, { align: 'right' })
    }
    L.y += 8.4
  }

  L.h2('Severity')
  L.para(
    'Severity follows the CVSS v3.1 qualitative bands, the same scale the ' +
    'National Vulnerability Database uses. Each band has a numeric weight, and ' +
    'those weights are what the score is built from - so severity here is not a ' +
    'label, it is arithmetic.', { size: 8, lead: 4 })
  for (const s of SEV_ORDER) {
    L.need(13)
    const y0 = L.y
    const [desc, cvss] = SEVERITY_INFO[s]
    L.pill(M, y0 + 0.6, s.toUpperCase(), SEV_RGB[s])
    L.set(6.8, 'normal', MUTED)
    doc.text(enc(cvss), M + 24, y0 + 0.6)
    L.set(7, 'bold', INK)
    doc.text(enc(`weight ${CVSS_WEIGHTS[s].toFixed(1)}`), M + 46, y0 + 0.6)
    L.set(7.4, 'normal', BODY)
    const lines = doc.splitTextToSize(enc(desc), CW - 70)
    doc.text(lines.slice(0, 2), M + 70, y0 + 0.6)
    L.y += Math.max(7, Math.min(lines.length, 2) * 3.6 + 3.4)
  }

  L.h2('Where a finding lives, and what is graded')
  L.para(
    'Every finding is classified by the part of the repository it was found in, ' +
    'and the grade is computed from shipped source only. Nothing is hidden - ' +
    'tests, docs and examples are reported in full and appear in the appendix - ' +
    'but they are not scored. The reason is measurable: on one well-known HTTP ' +
    'library, 505 of 571 findings came from its test suite and 387 of those were ' +
    '"assert used", which is exactly what a test file is supposed to contain. ' +
    'Grading those as defects gave one of the best-maintained libraries in ' +
    'Python an F.', { size: 8, lead: 4 })
  for (const [a, desc] of Object.entries(AREA_INFO)) {
    L.need(9)
    L.set(7.4, 'bold', a === 'source' ? ACCENT : INK)
    doc.text(enc(a), M, L.y)
    L.set(7.4, 'normal', BODY)
    doc.text(enc(desc), M + 22, L.y)
    L.y += 5
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. How the score was computed
// ─────────────────────────────────────────────────────────────────────────────
function scoring(L, report) {
  const { doc } = L
  L.section(3, 'How this score was computed',
    'The full calculation, with this repository\'s own numbers substituted, so ' +
    'the grade can be checked by hand rather than taken on trust.')

  L.y += 3
  gradeScale(L, report)

  const sev = report.severity || {}
  const loc = report.source_loc || 0
  const exposure = SEV_ORDER.reduce((a, k) => a + CVSS_WEIGHTS[k] * (sev[k] || 0), 0)
  const klocRaw = loc / 1000
  const kloc = Math.max(loc, MIN_LOC) / 1000
  const density = exposure / kloc
  const score = Math.round(100 * Math.exp(-density / DECAY_K))

  L.h2('Step 1 - risk exposure')
  L.para(
    'Each graded finding contributes the weight of its severity band. Only ' +
    'findings in shipped source are counted.', { size: 8, lead: 4 })
  const rows = SEV_ORDER.filter(k => (sev[k] || 0) > 0)
  if (!rows.length) {
    L.line('No graded findings, so exposure is 0.', { size: 7.8, color: MUTED })
  }
  for (const k of rows) {
    L.need(8)
    L.pill(M, L.y, k.toUpperCase(), SEV_RGB[k])
    L.set(7.6, 'normal', BODY)
    doc.text(enc(`${sev[k]} x ${CVSS_WEIGHTS[k].toFixed(1)}`), M + 26, L.y)
    L.set(7.6, 'bold', INK)
    doc.text(enc((CVSS_WEIGHTS[k] * sev[k]).toFixed(1)), M + 58, L.y, { align: 'right' })
    const share = exposure > 0 ? (CVSS_WEIGHTS[k] * sev[k]) / exposure : 0
    L.bar(M + 64, L.y - 2, CW - 64 - 18, 2.6, share, SEV_RGB[k])
    L.set(6.8, 'normal', MUTED)
    doc.text(enc(`${Math.round(share * 100)}%`), PW - M, L.y, { align: 'right' })
    L.y += 6.4
  }
  L.y += 1
  L.rule(1); L.y += 5
  L.set(8.2, 'bold', INK)
  doc.text('Total weighted exposure', M, L.y)
  doc.text(enc(exposure.toFixed(1)), PW - M, L.y, { align: 'right' })
  L.y += 8

  L.h2('Step 2 - density per thousand lines')
  const floored = loc < MIN_LOC
  L.para(
    `This repository has ${loc.toLocaleString()} lines of non-blank, ` +
    `non-comment source` +
    (floored
      ? `, which is below the ${MIN_LOC}-line floor. The divisor is held at ` +
        `${MIN_LOC} lines: one finding in twenty lines is not "fifty findings ` +
        `per thousand lines", and without the floor a tiny repository would ` +
        `swing between extremes on a single finding.`
      : `, so the divisor is ${klocRaw.toFixed(2)} thousand.`),
    { size: 8, lead: 4 })
  L.set(9, 'bold', INK)
  doc.text(enc(`density = ${exposure.toFixed(1)} / ${kloc.toFixed(2)} = ` +
               `${density.toFixed(2)} weighted findings per KLOC`), M, L.y)
  L.y += 9

  L.h2('Step 3 - exponential decay to 0-100')
  L.para(
    `score = 100 x e^(-density / ${DECAY_K}). Decay rather than subtraction, ` +
    `because a linear penalty hits zero and then stops telling bad apart from ` +
    `much worse; with decay every additional finding still moves the score, ` +
    `with diminishing effect.`, { size: 8, lead: 4 })
  L.set(9, 'bold', INK)
  doc.text(enc(`score = 100 x e^(-${density.toFixed(2)} / ${DECAY_K}) = ` +
               `${score}  ->  grade ${report.grade}`), M, L.y)
  L.y += 8
  if (score !== report.score) {
    L.para(
      `Note: recomputed here as ${score}, reported as ${report.score}. The ` +
      `reported value is authoritative; the difference is rounding in the ` +
      `severity totals carried into this document.`,
      { size: 7.2, color: MUTED, lead: 3.4 })
  }

}

/** The 0-100 scale with this repository's score marked on it. */
function gradeScale(L, report) {
  const { doc } = L
  L.label('Where this repository falls')
  const sx = M, sw = CW, sy = L.y + 1
  const segs = [['F', 0, 40], ['D', 40, 60], ['C', 60, 75], ['B', 75, 90], ['A', 90, 100]]
  for (const [g, a2, b2] of segs) {
    doc.setFillColor(...(GRADE_RGB[g] || MUTED))
    doc.rect(sx + (a2 / 100) * sw, sy, ((b2 - a2) / 100) * sw, 5, 'F')
    L.set(6.4, 'bold', PAPER)
    doc.text(enc(g), sx + ((a2 + b2) / 200) * sw, sy + 3.6, { align: 'center' })
  }
  const mx = sx + (Math.max(0, Math.min(100, report.score)) / 100) * sw
  doc.setFillColor(...INK)
  doc.triangle(mx, sy - 1.2, mx - 2, sy - 4.4, mx + 2, sy - 4.4, 'F')
  L.set(7, 'bold', INK)
  doc.text(enc(String(report.score)),
           Math.min(Math.max(mx, M + 5), PW - M - 5), sy - 5.8, { align: 'center' })
  L.y = sy + 10
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. Severity profile
// ─────────────────────────────────────────────────────────────────────────────
function severityProfile(L, report) {
  const { doc } = L
  L.section(4, 'Severity profile',
    'The graded column counts shipped source only and is what the score is built ' +
    'from. The reported column counts everything the scanners found, including ' +
    'tests, docs and examples.')

  const sev = report.severity || {}
  const all = report.severity_all || sev
  const maxAll = Math.max(1, ...SEV_ORDER.map(k => all[k] || 0))

  // Headline boxes
  const bw = (CW - 4 * 2.5) / 5
  SEV_ORDER.forEach((s, i) => {
    const x = M + i * (bw + 2.5)
    const rgb = SEV_RGB[s]
    doc.setDrawColor(...rgb)
    doc.setLineWidth(0.4)
    doc.setFillColor(...WASH)
    doc.roundedRect(x, L.y, bw, 19, 1.5, 1.5, 'FD')
    doc.setFillColor(...rgb)
    doc.rect(x, L.y, bw, 1.6, 'F')
    L.set(15, 'bold', rgb)
    doc.text(enc(String(sev[s] || 0)), x + bw / 2, L.y + 11.5, { align: 'center' })
    L.set(6, 'bold', MUTED)
    doc.text(enc(s.toUpperCase()), x + bw / 2, L.y + 16.4, { align: 'center' })
  })
  L.y += 24

  L.h2('Graded against reported')
  for (const s of SEV_ORDER) {
    L.need(10)
    const gN = sev[s] || 0, aN = all[s] || 0
    L.pill(M, L.y, s.toUpperCase(), SEV_RGB[s])
    const bx = M + 26, bwid = CW - 26 - 34
    // Reported total as the track, graded portion filled: the gap between them
    // IS the tests/docs share, visible without a second chart.
    L.bar(bx, L.y - 2.4, bwid, 3.4, aN / maxAll, [218, 220, 228])
    L.bar(bx, L.y - 2.4, bwid, 3.4, gN / maxAll, SEV_RGB[s], null)
    L.set(7.2, 'bold', INK)
    doc.text(enc(String(gN)), PW - M - 16, L.y, { align: 'right' })
    L.set(7.2, 'normal', MUTED)
    doc.text(enc(`/ ${aN}`), PW - M, L.y, { align: 'right' })
    L.y += 7.4
  }
  L.y += 1
  L.set(6.8, 'normal', MUTED)
  doc.text(enc('Filled = graded (shipped source).  Grey = also reported in tests, docs or examples.'), M, L.y)
  L.y += 7

  const graded = report.graded_on ?? 0
  const total = report.total ?? 0
  L.para(
    graded === total
      ? `All ${total} findings are in shipped source, so the graded and reported ` +
        `columns are identical.`
      : `${graded} of ${total} findings are in shipped source and carry weight. ` +
        `The remaining ${total - graded} are reported in full in the appendix but ` +
        `contribute nothing to the score.`,
    { size: 8, lead: 4 })
}

// ─────────────────────────────────────────────────────────────────────────────
// 5. Where the findings live
// ─────────────────────────────────────────────────────────────────────────────
function whereFindings(L, report) {
  const { doc } = L
  L.section(5, 'Where the findings live',
    'The distribution across the repository. A large share outside shipped ' +
    'source usually means a thorough test suite rather than a problem.')

  const byArea = report.by_area || {}
  const total = Object.values(byArea).reduce((a, b) => a + b, 0) || 1
  const order = ['source', 'test', 'docs', 'example']
    .filter(a => byArea[a]).concat(Object.keys(byArea).filter(a => !AREA_INFO[a]))

  // Single stacked bar: the proportions, at a glance.
  L.need(22)
  const sy = L.y
  let cx = M
  const AREA_RGB = { source: ACCENT, test: [120, 190, 160], docs: [190, 175, 120],
                     example: [175, 160, 200] }
  for (const a of order) {
    const w = (byArea[a] / total) * CW
    doc.setFillColor(...(AREA_RGB[a] || MUTED))
    doc.rect(cx, sy, w, 7, 'F')
    if (w > 14) {
      L.set(6.4, 'bold', PAPER)
      doc.text(enc(`${Math.round((byArea[a] / total) * 100)}%`), cx + w / 2, sy + 4.7,
               { align: 'center' })
    }
    cx += w
  }
  L.y = sy + 13

  for (const a of order) {
    L.need(11)
    doc.setFillColor(...(AREA_RGB[a] || MUTED))
    doc.circle(M + 1.6, L.y - 1.2, 1.6, 'F')
    L.set(8, 'bold', INK)
    doc.text(enc(a), M + 6, L.y)
    L.set(8, 'bold', a === 'source' ? INK : MUTED)
    doc.text(enc(`${byArea[a]}`), M + 32, L.y, { align: 'right' })
    L.set(7.2, 'normal', BODY)
    const d = AREA_INFO[a] || 'Other files in the repository.'
    doc.text(doc.splitTextToSize(enc(d), CW - 38).slice(0, 1), M + 38, L.y)
    L.y += 6.6
  }
  L.y += 2
}

// ─────────────────────────────────────────────────────────────────────────────
// 6. Scanner coverage
// ─────────────────────────────────────────────────────────────────────────────
function scannerCoverage(L, report) {
  const { doc } = L
  L.section(6, 'Scanner coverage',
    'All nine scanners and what each one is responsible for. Seven wrap an ' +
    'established open-source tool; the last two are RepoSage\'s own analyses and ' +
    'are described in detail in section 9.')

  const by = report.by_scanner || {}
  const max = Math.max(1, ...SCANNERS.map(s => by[s.id] || 0))

  for (const s of SCANNERS) {
    const n = by[s.id] || 0
    L.need(24)
    const y0 = L.y
    if (s.own) {
      doc.setFillColor(248, 248, 253)
      doc.rect(M - 2, y0 - 5, CW + 4, 22, 'F')
      doc.setFillColor(...ACCENT)
      doc.rect(M - 2, y0 - 5, 1.2, 22, 'F')
    }
    L.set(9.2, 'bold', INK)
    doc.text(enc(s.name), M, L.y)
    const nameW = doc.getTextWidth(enc(s.name))
    L.set(7, 'normal', MUTED)
    doc.text(enc(s.own ? 'RepoSage original' : s.tool), M + nameW + 3, L.y)
    L.set(10, 'bold', n > 0 ? ACCENT : [190, 192, 200])
    doc.text(enc(String(n)), PW - M, L.y, { align: 'right' })
    L.y += 2.6
    L.bar(M, L.y, CW - 14, 2.2, n / max, n > 0 ? ACCENT : [228, 229, 235])
    L.y += 5.4
    L.para(s.detects, { size: 7.4, lead: 3.5, color: BODY, gap: 1.6 })
    L.set(7.2, 'bold', [90, 92, 104])
    doc.text('What to do:', M, L.y)
    L.set(7.2, 'normal', BODY)
    for (const ln of doc.splitTextToSize(enc(s.fix), CW - 20)) {
      doc.text(ln, M + 20, L.y); L.y += 3.4
    }
    L.y = Math.max(L.y, y0 + 18) + 3.4
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 7. Priority findings
// ─────────────────────────────────────────────────────────────────────────────
function priorityFindings(L, report, groups) {
  const { doc } = L
  const picks = groups.filter(g => g.graded > 0 && g.severity !== 'info')
    .slice(0, 8)
  const fallback = picks.length ? picks : groups.slice(0, 8)

  L.section(7, 'Priority findings',
    fallback.length
      ? `The ${fallback.length} issue types that carry the most weight, worst ` +
        `first. Each card gives the rule, what it means, where it occurs and ` +
        `what to do. Everything else is in the index and the appendix.`
      : 'Nothing in shipped source rose above informational severity.')

  if (!fallback.length) {
    L.para('No priority findings. The scanners reported nothing above ' +
           'informational severity in shipped code.', { size: 8.4 })
    return
  }

  for (const g of fallback) {
    const info = RULE_INFO[g.rule]
    const locs = g.items.slice(0, 3)
    const extra = g.items.length - locs.length
    // Height estimate so a card is not split down the middle.
    L.need(46)

    const y0 = L.y - 5
    const rgb = SEV_RGB[g.severity] || MUTED
    doc.setFillColor(252, 252, 253)
    doc.setDrawColor(...FAINT)
    doc.setLineWidth(0.2)
    doc.roundedRect(M, y0, CW, 6, 1.5, 1.5, 'FD')     // resized below
    const cardTop = y0

    L.y += 0.5
    const pw = L.pill(M + 3, L.y, g.severity.toUpperCase(), rgb)
    L.set(9.4, 'bold', INK)
    doc.text(enc(SC[g.scanner]?.name || g.scanner), M + 5 + pw, L.y)
    if (g.rule) {
      const w2 = doc.getTextWidth(enc(SC[g.scanner]?.name || g.scanner))
      L.set(9.4, 'bold', ACCENT)
      doc.text(enc(g.rule), M + 8 + pw + w2, L.y)
    }
    L.set(7, 'normal', MUTED)
    doc.text(enc(`${g.items.length} occurrence${g.items.length === 1 ? '' : 's'}`),
             PW - M - 3, L.y, { align: 'right' })
    L.y += 5

    if (info) {
      L.set(9.2, 'bold', INK)
      doc.text(enc(info[0]), M + 3, L.y)
      L.y += 4.8
      L.para(info[1], { size: 8, lead: 3.9, x: M + 3, w: CW - 6, gap: 2.8 })
    }
    L.para(g.message, { size: 8.2, lead: 4, x: M + 3, w: CW - 6,
                        color: info ? [80, 82, 95] : BODY, gap: 3 })

    L.set(7, 'bold', [112, 115, 128])
    doc.text('WHERE', M + 3, L.y)
    L.y += 3.8
    for (const f of locs) {
      L.set(7, 'normal', [70, 72, 84])
      const loc = `${f.file || '-'}${f.line ? `:${f.line}` : ''}`
      doc.text(enc(loc.length > 92 ? '...' + loc.slice(-89) : loc), M + 5, L.y)
      L.y += 3.6
    }

    if (extra > 0) {
      L.set(6.8, 'italic', MUTED)
      doc.text(enc(`+ ${extra} more - see the appendix`), M + 5, L.y)
      L.y += 3.6
    }

    // The code itself, for the first occurrence that has it. A rule id and a
    // line number make the reader go and look it up; this saves the trip.
    const withCode = g.items.find(f => f.code?.length)
    if (withCode) {
      L.y += 1.4
      L.set(7, 'bold', [112, 115, 128])
      doc.text(enc(`CODE  ${withCode.file}:${withCode.line}`), M + 3, L.y)
      L.y += 3.8
      L.code(withCode.code, withCode.code_start, withCode.line, rgb,
             { x: M + 3, w: CW - 6 })
    }

    const fix = info ? info[2] : SC[g.scanner]?.fix
    if (fix) {
      L.y += 1.4
      doc.setDrawColor(...FAINT)
      doc.setLineWidth(0.2)
      doc.line(M + 3, L.y - 2, PW - M - 3, L.y - 2)
      L.y += 1.6
      L.set(7, 'bold', ACCENT)
      doc.text('WHAT TO DO', M + 3, L.y)
      L.set(7.2, 'normal', BODY)
      for (const ln of doc.splitTextToSize(enc(fix), CW - 30)) {
        doc.text(ln, M + 25, L.y); L.y += 3.5
      }
    }

    // Redraw the card outline now that the real height is known. The first
    // roundedRect above only reserved the top edge.
    const h = L.y - cardTop + 1.5
    doc.setDrawColor(...FAINT)
    doc.setLineWidth(0.2)
    doc.roundedRect(M, cardTop, CW, h, 1.5, 1.5, 'S')
    doc.setFillColor(...rgb)
    doc.rect(M, cardTop + 1, 1.3, h - 2, 'F')
    L.y += 7
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 8. Complete issue index
// ─────────────────────────────────────────────────────────────────────────────
function issueIndex(L, report, groups) {
  const { doc } = L
  L.section(8, 'Complete issue index',
    `All ${groups.length} issue type${groups.length === 1 ? '' : 's'}, grouped ` +
    `by severity. Every row links to that issue's entry in the appendix, where ` +
    `each occurrence is listed with its file and line.`)

  const links = []            // resolved once the appendix knows its pages
  const ROW = 6.4
  let lastSev = null

  for (const g of groups) {
    const header = g.severity !== lastSev
    L.need(ROW + (header ? 7 : 0))
    if (header) {
      lastSev = g.severity
      L.y += 2
      const rgb = SEV_RGB[g.severity] || MUTED
      doc.setFillColor(...rgb)
      doc.rect(M, L.y - 3.4, CW, 4.6, 'F')
      L.set(6.6, 'bold', PAPER)
      doc.text(enc(g.severity.toUpperCase()), M + 2, L.y)
      const n = groups.filter(x => x.severity === g.severity)
        .reduce((a, x) => a + x.items.length, 0)
      doc.text(enc(`${n} finding${n === 1 ? '' : 's'}`), PW - M - 2, L.y,
               { align: 'right' })
      L.y += 6.4
    }

    const top = L.y - 3.6
    if (groups.indexOf(g) % 2 === 0) {
      doc.setFillColor(250, 250, 252)
      doc.rect(M, top, CW, ROW, 'F')
    }
    const CX_RULE = M + 31, CX_MSG = M + 74
    const CX_AREA = PW - M - 30, CX_CNT = PW - M - 15
    L.set(7.4, 'bold', [78, 80, 92])
    doc.text(fit(doc, SC[g.scanner]?.name || g.scanner, CX_RULE - M - 3), M + 1.5, L.y)
    L.set(7.4, 'bold', ACCENT)
    doc.text(fit(doc, g.rule || '', CX_MSG - CX_RULE - 3), CX_RULE, L.y)
    L.set(7.4, 'normal', [40, 42, 52])
    doc.text(fit(doc, g.message || '', CX_AREA - CX_MSG - 3), CX_MSG, L.y)
    if (g.areaShort) {
      L.set(5.8, 'normal', [150, 153, 165])
      doc.text(fit(doc, g.areaShort, CX_CNT - CX_AREA - 2), CX_AREA, L.y)
    }
    L.set(6.4, 'normal', MUTED)
    doc.text(enc(`x${g.items.length}`), CX_CNT, L.y, { align: 'right' })
    L.set(6.4, 'bold', ACCENT)
    doc.text('view', PW - M, L.y, { align: 'right' })
    links.push({ page: L.page, x: M, y: top, w: CW, h: ROW, group: g })
    L.y += ROW
  }
  return links
}

// ─────────────────────────────────────────────────────────────────────────────
// 9. RepoSage's own analysis
// ─────────────────────────────────────────────────────────────────────────────
function ownAnalysis(L, report) {
  const { doc } = L
  L.section(9, "RepoSage's own analysis",
    'Seven of the nine scanners run an established tool and normalise its ' +
    'output. The two described here are built into RepoSage and exist because a ' +
    'whole class of real defect is invisible to every one of those tools: they ' +
    'read one file at a time, and these defects are properties of the repository ' +
    'as a whole.')

  const by = report.by_scanner || {}

  L.h2('Architecture Health - graph-shaped defects')
  L.para(
    'RepoSage already builds two graphs to render the architecture view: a ' +
    'module dependency graph from the import statements, and a function call ' +
    'graph resolved through a symbol table. This scanner asks questions of those ' +
    'graphs that no single file can answer.', { size: 8, lead: 4 })
  const archRules = ['ARCH001', 'ARCH002', 'ARCH003', 'ARCH004']
  ruleTable(L, archRules, report)
  L.para(
    'Thresholds come from the repository\'s own distribution - a module is a god ' +
    'module when it exceeds two standard deviations above that repository\'s mean ' +
    'fan-in and fan-out, not when it crosses a number somebody picked. The same ' +
    'absolute threshold would call every module in a large project an outlier.',
    { size: 7.6, lead: 3.7, color: [90, 92, 104] })
  L.set(7.6, 'bold', INK)
  doc.text(enc(`Findings in this repository: ${by.arch || 0}`), M, L.y)
  L.y += 8

  L.h2('Exception Flow - interprocedural exception propagation')
  L.para(
    'This builds an exception propagation graph: the call graph annotated with ' +
    'which exception types can escape each function, solved as a fixpoint so ' +
    'recursion and mutual recursion need no special case. It then reports ' +
    'exception-handling anti-patterns that require following a failure across ' +
    'function and module boundaries.', { size: 8, lead: 4 })
  ruleTable(L, ['EXC001', 'EXC002', 'EXC003', 'EXC004', 'EXC005'], report)
  L.set(7.6, 'bold', INK)
  doc.text(enc(`Findings in this repository: ${by.excflow || 0}`), M, L.y)
  L.y += 8

  L.need(60)
  L.h2('Why this is worth running')
  L.para(
    'Souza, Coelho, Correia, Lima, Teixeira and Neto studied 1,649 confirmed ' +
    'exception-handling bugs across 550 open-source Python projects ' +
    '("Slithering Through Exception Handling Bugs in Python: Understanding Root ' +
    'Causes, Symptoms, and Fixes", 2025). Unhandled Exception was the single ' +
    'largest root cause at 50.64%, and Failure to Handle Expected Exceptions the ' +
    'most common symptom at 33.97%.', { size: 7.8, lead: 3.8 })
  L.para(
    'de Padua and Shang detected 19 exception-handling anti-patterns across 16 ' +
    'systems and found five to be prevalent: Unhandled Exceptions, Catch Generic, ' +
    'Unreachable Handler, Over-catch and Destructive Wrapping ("Studying the ' +
    'Prevalence of Exception Handling Anti-Patterns", ICSME 2017). Four of those ' +
    'five are flow anti-patterns - undecidable from a single file - and their ' +
    'tooling targets Java and C#.', { size: 7.8, lead: 3.8 })
  L.para(
    'Python has no equivalent. ruff (BLE001, B904, the TRY rules), pylint (W0703) ' +
    'and tryceratops each reason about one try statement in one file, so none of ' +
    'them can decide whether a handler is reachable, whether anything handles a ' +
    'given failure, or whether a cleanup block can destroy the exception already ' +
    'in flight. The five rules above answer exactly those questions.',
    { size: 7.8, lead: 3.8 })

  L.need(42)
  L.h2('How it stays sound')
  L.para(
    'A whole-program exception analysis for a dynamically typed language is ' +
    'undecidable in general, which is why nobody ships one. The restriction that ' +
    'makes this tractable: every rule is anchored on exception classes the ' +
    'audited repository defines in its own shipped code. For those the repository ' +
    'is a closed world - every raise and every except clause that can mention ' +
    'MyPkgError is inside the code being audited, because a third-party function ' +
    'cannot raise a class it has never seen. The analysis stays approximate about ' +
    'control flow but is near-exact about the exception lattice, so it ' +
    'under-reports rather than inventing findings.', { size: 7.8, lead: 3.8 })
  L.para(
    'Precision was checked against ten widely used projects - requests, click, ' +
    'flask, invoke, python-fire, typer, httpx, rich, scrapy and black - with ' +
    'every finding verified by hand. Three false-positive classes were traced to ' +
    'their cause and fixed rather than thresholded away: name collisions (one ' +
    'library defines its own SSLError and also imports urllib3\'s under the same ' +
    'bare name), generators (an exception can be thrown in at a yield, so ' +
    '"nothing here raises it" proves nothing), and test fixtures (one project\'s ' +
    'tests define ten throwaway exception classes deliberately). Recall is ' +
    'checked against a fixture repository containing one instance of each defect, ' +
    'where all five rules fire and the correctly-handled control case stays ' +
    'silent.', { size: 7.8, lead: 3.8 })
}

/** Compact rule reference: name, what it means, what to do. */
function ruleTable(L, rules, report) {
  const { doc } = L
  for (const r of rules) {
    const info = RULE_INFO[r]
    if (!info) continue
    L.need(18)
    L.set(7.4, 'bold', ACCENT)
    doc.text(enc(r), M, L.y)
    L.set(7.8, 'bold', INK)
    doc.text(enc(info[0]), M + 17, L.y)
    L.y += 4
    L.para(info[1], { size: 7.2, lead: 3.4, x: M + 17, w: CW - 17, gap: 1.6,
                      color: [84, 86, 98] })
  }
  L.y += 2
}

// ─────────────────────────────────────────────────────────────────────────────
// Appendix A — every occurrence
// ─────────────────────────────────────────────────────────────────────────────
function appendix(L, report, groups, indexPage) {
  const { doc } = L
  L.newPage()
  L.mark('Appendix A - All findings')
  L.st.toc.push({ num: 'A', title: 'Appendix: all findings', page: L.page })

  L.set(7.2, 'bold', ACCENT)
  doc.text('APPENDIX A', M, L.y)
  L.y += 7
  L.set(16, 'bold', INK)
  doc.text('All findings', M, L.y)
  L.y += 3
  doc.setDrawColor(...ACCENT)
  doc.setLineWidth(0.8)
  doc.line(M, L.y, M + 14, L.y)
  L.y += 6
  L.para(
    `Every finding the audit produced, grouped by issue type and ordered worst ` +
    `first: ${(report.findings || []).length} findings in ${groups.length} ` +
    `issue types. Locations are relative to the repository root.`,
    { size: 8.4, lead: 4.2 })
  L.y += 2

  for (const g of groups) {
    L.need(20)
    g.page = L.page                       // the index links here
    const rgb = SEV_RGB[g.severity] || MUTED

    doc.setFillColor(...rgb)
    doc.roundedRect(M, L.y - 3.9, CW, 5.6, 1, 1, 'F')
    L.set(8, 'bold', PAPER)
    doc.text(enc(`${g.severity.toUpperCase()}   ${SC[g.scanner]?.name || g.scanner}` +
                 `${g.rule ? '   ' + g.rule : ''}`), M + 2.5, L.y)
    doc.text(enc(`${g.items.length} occurrence${g.items.length === 1 ? '' : 's'}` +
                 (g.areaLabel ? `  ·  ${g.areaLabel}` : '')),
             PW - M - 2.5, L.y, { align: 'right' })
    if (indexPage) doc.link(M, L.y - 3.9, CW, 5.6, { pageNumber: indexPage })
    L.y += 6.4

    L.para(g.message, { size: 8, lead: 3.9, x: M + 1, w: CW - 2, gap: 2.8,
                        color: [66, 68, 80] })

    const info = RULE_INFO[g.rule]
    if (info) {
      L.set(6.6, 'bold', ACCENT)
      doc.text('FIX', M + 1, L.y)
      L.set(7, 'normal', [84, 86, 98])
      for (const ln of doc.splitTextToSize(enc(info[2]), CW - 14)) {
        L.need(4.4)
        doc.text(ln, M + 11, L.y); L.y += 3.4
      }
      L.y += 1.4
    }

    // Code is shown for the first few occurrences only. An issue with 140
    // occurrences would otherwise contribute 700 lines of near-identical
    // listing and bury everything else in the appendix.
    let shownCode = 0
    g.items.forEach((f, i) => {
      L.need(5.2)
      if (i % 2 === 0) {
        doc.setFillColor(249, 249, 251)
        doc.rect(M, L.y - 3.1, CW, 4.8, 'F')
      }
      L.set(6.8, 'normal', [78, 80, 92])
      const loc = `${f.file || '-'}${f.line ? `:${f.line}` : ''}`
      doc.text(enc(loc.length > 96 ? '...' + loc.slice(-93) : loc), M + 1.5, L.y)
      if (f.confidence) {
        L.set(6.4, 'normal', MUTED)
        doc.text(enc(`confidence: ${f.confidence}`), PW - M - 1.5, L.y, { align: 'right' })
      }
      L.y += 4.8
      if (f.code?.length && shownCode < APPENDIX_CODE_LIMIT) {
        shownCode++
        L.code(f.code, f.code_start, f.line, rgb, { x: M + 4, w: CW - 8 })
      }
    })
    if (shownCode && g.items.length > shownCode) {
      L.set(6.4, 'italic', MUTED)
      doc.text(enc(`Code shown for the first ${shownCode} of ` +
                   `${g.items.length} occurrences.`), M + 4, L.y)
      L.y += 4
    }
    L.y += 5
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Footers, stamped once the total page count is known
// ─────────────────────────────────────────────────────────────────────────────
function stampFooters(doc, report, marks = []) {
  const n = doc.getNumberOfPages()
  const when = new Date().toLocaleDateString()
  const labelFor = (page) => {
    let out = ''
    for (const m of marks) { if (m.page <= page) out = m.label; else break }
    return out
  }
  for (let i = 1; i <= n; i++) {
    doc.setPage(i)
    if (i === 1) continue                 // the cover carries its own footer

    doc.setFontSize(6.8)
    doc.setFont('helvetica', 'normal')
    doc.setTextColor(...MUTED)
    doc.text(enc(report.repo || ''), M, M - 4)
    const lab = labelFor(i)
    if (lab) doc.text(enc(lab), PW - M, M - 4, { align: 'right' })
    doc.setDrawColor(...FAINT)
    doc.setLineWidth(0.2)
    doc.line(M, M - 1.6, PW - M, M - 1.6)

    doc.setDrawColor(...FAINT)
    doc.setLineWidth(0.2)
    doc.line(M, PH - 11, PW - M, PH - 11)
    doc.setFontSize(6.4)
    doc.setFont('helvetica', 'normal')
    doc.setTextColor(...MUTED)
    doc.text(enc(`RepoSage  ·  ${report.repo || ''}  ·  grade ${report.grade} ` +
                 `(${report.score}/100)`), M, PH - 7)
    doc.text(enc(when), PW - M, PH - 7, { align: 'right' })
    doc.setFont('helvetica', 'bold')
    doc.setTextColor(...[110, 112, 126])
    doc.text(enc(`${i} / ${n}`), PW / 2, PH - 7, { align: 'center' })
  }
}

/**
 * Turn the index rows into real links. The appendix is laid out after the
 * index, so the destination pages only exist by this point; jsPDF lets us go
 * back to a finished page and add the annotations.
 */
function resolveIndexLinks(doc, links) {
  const back = doc.internal.getCurrentPageInfo().pageNumber
  for (const l of links) {
    if (!l.group.page) continue
    doc.setPage(l.page)
    doc.link(l.x, l.y, l.w, l.h, { pageNumber: l.group.page })
  }
  doc.setPage(back)
}
