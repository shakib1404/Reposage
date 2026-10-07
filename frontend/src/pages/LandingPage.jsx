/**
 * LandingPage — the unauthenticated home page.
 *
 * Replaces the old bare AuthPage: a visitor now gets the explanation of what
 * RepoSage does *and* the sign-in form on the same screen, so there is no
 * "log in to find out what this is" dead end. The form itself is AuthCard.
 */
import { useState, useEffect, useRef } from 'react'
import AuthCard from '../components/AuthCard'
import ThemeToggle from '../components/ThemeToggle'

// ── The eight workflow stages, in the order the app's tabs run them ────────
const STAGES = [
  {
    n: '1', title: 'Describe the task',
    body: <>Type what you want done in plain English — <em>“sentiment analysis of movie reviews”</em>. Attach your own images, CSVs or PDFs and they travel with the job into the sandbox.</>,
  },
  {
    n: '2', title: 'Get a ranked shortlist',
    body: <>An LLM normalises your phrasing into a search query, GitHub is searched broadly, then nine Python repos are scored on <strong>semantic fit</strong>, <strong>popularity</strong> and — unusually — <strong>runnability</strong>: does it declare dependencies, ship an entrypoint, still get commits?</>,
  },
  {
    n: '3', title: 'Read the repository',
    body: <>The winner is cloned and parsed. You get a hierarchical code tree (module › class › method), a function call graph, a module dependency graph, and a file listing that matches <code>github.com</code> exactly.</>,
  },
  {
    n: '4', title: 'See the architecture',
    body: <>A layered architecture diagram is generated from the repo's real import graph — not guessed from the README — so you can see how the pieces actually connect before you run anything.</>,
  },
  {
    n: '5', title: 'Run it, autonomously',
    body: <>Dependencies are installed and the entrypoint executed in an isolated workspace. When it crashes, the traceback goes back to the model, which <strong>rewrites the offending files and retries</strong> — a real self-healing loop, not a single attempt.</>,
  },
  {
    n: '6', title: 'Collect the output',
    body: <>Live streamed logs, the exit code, every artifact the run produced — images, CSVs, models — listed and downloadable, plus a written summary of what actually happened.</>,
  },
  {
    n: '7', title: 'Audit the code',
    body: <>Secret scanning, linting, type checking and complexity analysis run over the clone and collapse into a letter grade with a downloadable PDF report. Vendored <code>venv/</code> trees are excluded, so the findings are about the author's code.</>,
  },
  {
    n: '8', title: 'Re-point and revisit',
    body: <>Send an already-analysed repo at a brand-new task, ask questions about its code in chat, or reopen any past run from History with every graph and result intact.</>,
  },
]

const FEATURES = [
  {
    ico: '🎯', tint: 'var(--accent-dim)', color: 'var(--accent)',
    title: 'Ranking that cares if it runs',
    body: 'Most search returns whatever is popular. Every shortlisted repo here is probed for dependency manifests, entrypoints, Docker support and commit recency first — so the repo you pick is one that can actually start.',
  },
  {
    ico: '🌳', tint: 'var(--green-dim)', color: 'var(--green)',
    title: 'Two views of the code',
    body: 'A hierarchical code tree for structure — modules, classes, methods, call edges — next to a byte-exact file listing of what was committed. Verified file-for-file against the GitHub API.',
  },
  {
    ico: '🔁', tint: 'var(--purple-dim)', color: 'var(--purple)',
    title: 'Self-healing execution',
    body: 'A missing package, a wrong path, a Python-version break: the failure is diagnosed, files are patched on disk, and the run is attempted again — repeatedly, until it passes or the budget runs out.',
  },
  {
    ico: '🛡️', tint: 'var(--yellow-dim)', color: 'var(--yellow)',
    title: 'Audit with a paper trail',
    body: 'Hardcoded secrets, type errors, lint violations and hotspots of complexity, scored into a grade and exported as a PDF you can hand to someone else.',
  },
  {
    ico: '💬', tint: 'var(--accent-dim)', color: 'var(--accent)',
    title: 'Chat with the codebase',
    body: 'Retrieval runs over the actual clone, so answers cite real functions in real files instead of paraphrasing the README back at you.',
  },
  {
    ico: '🔎', tint: 'var(--red-dim)', color: 'var(--red)',
    title: 'Copy detection',
    body: 'Compare a repository against a corpus to surface duplicated logic — structural similarity, not just matching text.',
  },
]

// Fake-but-representative log lines for the hero terminal.
const TERM_LINES = [
  { c: 'var(--txt3)',  t: '$', m: 'reposage "sentiment analysis of movie reviews"' },
  { c: 'var(--accent)', t: '›', m: 'normalised query → "sentiment analysis"' },
  { c: 'var(--accent)', t: '›', m: 'scanning GitHub · 80 candidates' },
  { c: 'var(--purple)', t: '›', m: 'reranking · semantic + popularity + runnability' },
  { c: 'var(--green)',  t: '✓', m: 'cjhutto/vaderSentiment  ★ 4.6k  runnable 0.91' },
  { c: 'var(--accent)', t: '›', m: 'cloning · parsing 42 modules · 318 functions' },
  { c: 'var(--yellow)', t: '!', m: 'attempt 1 failed — ModuleNotFoundError: nltk' },
  { c: 'var(--purple)', t: '›', m: 'patching requirements.txt · retrying' },
  { c: 'var(--green)',  t: '✓', m: 'exit 0 in 41s · 2 artifacts · audit grade A' },
]

// ── Reveal-on-scroll ───────────────────────────────────────────────────────
function useReveal() {
  useEffect(() => {
    const els = [...document.querySelectorAll('.reveal')]
    const showAll = () => els.forEach(el => el.classList.add('in'))

    // .reveal starts at opacity 0, so anything that stops this hook from
    // running leaves half the page invisible. Bail out to "just show it"
    // whenever the observer isn't available.
    if (!('IntersectionObserver' in window)) { showAll(); return }

    const io = new IntersectionObserver(entries => {
      for (const e of entries) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target) }
      }
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.05 })
    els.forEach(el => io.observe(el))

    // Failsafe: the animation is decoration, the content is not. If anything
    // (a print/screenshot viewport resize, a container that never scrolls,
    // a browser quirk) stops the observer firing, reveal everything anyway.
    const failsafe = setTimeout(showAll, 2500)

    return () => { clearTimeout(failsafe); io.disconnect() }
  }, [])
}

// ── Hero terminal: reveals one line at a time, then holds ──────────────────
function Terminal() {
  const [shown, setShown] = useState(0)
  useEffect(() => {
    if (shown >= TERM_LINES.length) return
    const id = setTimeout(() => setShown(n => n + 1), shown === 0 ? 350 : 520)
    return () => clearTimeout(id)
  }, [shown])

  return (
    <div className="lp-term" aria-hidden="true">
      <div className="lp-term-bar">
        <span className="lp-term-dot" style={{ background: '#ff5f57' }} />
        <span className="lp-term-dot" style={{ background: '#febc2e' }} />
        <span className="lp-term-dot" style={{ background: '#28c840' }} />
        <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--txt3)', fontFamily: 'var(--mono)' }}>
          reposage — run
        </span>
      </div>
      <div className="lp-term-body" style={{ minHeight: 244 }}>
        {TERM_LINES.slice(0, shown).map((l, i) => (
          <div key={i} className="lp-term-line">
            <span style={{ color: l.c, flexShrink: 0, width: 10 }}>{l.t}</span>
            <span style={{ color: i === 0 ? 'var(--txt)' : 'var(--txt2)' }}>{l.m}</span>
          </div>
        ))}
        {shown >= TERM_LINES.length && (
          <div className="lp-term-line">
            <span style={{ color: 'var(--txt3)', width: 10, flexShrink: 0 }}>$</span>
            <span className="lp-caret" />
          </div>
        )}
      </div>
    </div>
  )
}

// ── Logo mark, shared by nav and footer ───────────────────────────────────
function Logo({ size = 30 }) {
  return (
    <div style={{
      width: size, height: size, borderRadius: size / 3.6,
      background: 'var(--grad)', flexShrink: 0,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      boxShadow: '0 4px 14px -4px color-mix(in srgb, var(--accent) 70%, transparent)',
    }}>
      <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none"
        stroke="white" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
      </svg>
    </div>
  )
}

export default function LandingPage({ onAuth }) {
  // A reset link lands here, not on a separate page — enter reset mode and
  // scroll the form into view so the token isn't silently ignored.
  const resetToken = new URLSearchParams(window.location.search).get('reset_token') || ''
  const [mode, setMode] = useState(resetToken ? 'reset' : 'login')
  const [scrolled, setScrolled] = useState(false)
  const authRef = useRef(null)

  useReveal()

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (resetToken) authRef.current?.scrollIntoView({ block: 'center' })
  }, [resetToken])

  /** Switch the form's mode and bring it into view — used by every CTA. */
  const jumpToAuth = m => {
    setMode(m)
    authRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  return (
    <div className="lp">
      <div className="lp-bg" />

      <div className="lp-shell">
        {/* ── Nav ─────────────────────────────────────────────────────── */}
        <nav className={`lp-nav${scrolled ? ' scrolled' : ''}`}>
          <div className="lp-wrap lp-nav-inner">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Logo size={28} />
              <span style={{ fontWeight: 700, fontSize: 15.5, letterSpacing: '-0.025em' }}>
                RepoSage
              </span>
            </div>
            <div className="lp-nav-links">
              <a href="#how">How it works</a>
              <a href="#features">Features</a>
              <a href="#safety">Safety</a>
            </div>
            <div style={{ flex: 1 }} />
            <ThemeToggle />
            <button className="lp-btn lp-btn-ghost lp-nav-signin" onClick={() => jumpToAuth('login')}>
              Sign in
            </button>
            <button className="lp-btn lp-btn-primary" onClick={() => jumpToAuth('register')}>
              Get started
            </button>
          </div>
        </nav>

        {/* ── Hero ────────────────────────────────────────────────────── */}
        <div className="lp-wrap">
          <section className="lp-hero">
            <div>
              <span className="lp-eyebrow">
                <span className="lp-dot" />
                Autonomous repository exploration &amp; execution
              </span>

              <h1 className="lp-h1">
                Describe the task.<br />
                <span className="lp-grad-text">It finds the repo, reads it, and runs it.</span>
              </h1>

              <p className="lp-sub">
                RepoSage searches GitHub for a project that solves your problem, ranks
                the results by whether they will actually <em>run</em>, maps the codebase,
                then executes it in a sandbox — fixing its own failures until it works.
              </p>

              <div className="lp-cta-row">
                <button className="lp-btn lp-btn-primary lp-btn-lg" onClick={() => jumpToAuth('register')}>
                  Create a free account
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
                  </svg>
                </button>
                <a className="lp-btn lp-btn-ghost lp-btn-lg" href="#how">
                  See how it works
                </a>
              </div>

              <Terminal />
            </div>

            {/* Auth lives in the hero — explanation and sign-up on one screen */}
            <div className="lp-auth-col" ref={authRef}>
              <AuthCard onAuth={onAuth} mode={mode} setMode={setMode} resetToken={resetToken} />
            </div>
          </section>

          {/* ── Stats ─────────────────────────────────────────────────── */}
          <div className="lp-stats reveal">
            {[
              ['9', 'ranked candidates per search'],
              ['8', 'stages from prompt to audit'],
              ['3', 'code graphs built per repo'],
              ['0', 'manual setup steps to run it'],
            ].map(([n, l]) => (
              <div className="lp-stat" key={l}>
                <div className="lp-stat-n lp-grad-text">{n}</div>
                <div className="lp-stat-l">{l}</div>
              </div>
            ))}
          </div>

          {/* ── How it works ──────────────────────────────────────────── */}
          <section className="lp-section" id="how" style={{ scrollMarginTop: 80 }}>
            <div className="lp-sec-head reveal">
              <div className="lp-kicker">How it works</div>
              <h2 className="lp-h2">Eight stages, one prompt to start them</h2>
              <p className="lp-sec-sub">
                Each stage is a tab in the app. You can stop at any of them — read the
                architecture and leave, or go all the way to a graded audit report.
              </p>
            </div>

            <div className="lp-pipe">
              {STAGES.map(s => (
                <article className="lp-stage reveal" key={s.n}>
                  <div className="lp-stage-n">{s.n}</div>
                  <div>
                    <h3>{s.title}</h3>
                    <p>{s.body}</p>
                  </div>
                </article>
              ))}
            </div>
          </section>

          {/* ── Features ──────────────────────────────────────────────── */}
          <section className="lp-section" id="features" style={{ scrollMarginTop: 80 }}>
            <div className="lp-sec-head reveal">
              <div className="lp-kicker">What makes it different</div>
              <h2 className="lp-h2">Built for repos you intend to run</h2>
              <p className="lp-sec-sub">
                Finding a repository is the easy half. Everything here is aimed at the
                harder half — understanding it, and getting it to execute.
              </p>
            </div>

            <div className="lp-feats">
              {FEATURES.map(f => (
                <article className="lp-feat reveal" key={f.title}>
                  <div className="lp-feat-ico" style={{ background: f.tint, color: f.color }}>
                    {f.ico}
                  </div>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </article>
              ))}
            </div>
          </section>

          {/* ── Safety ────────────────────────────────────────────────── */}
          <section id="safety" style={{ scrollMarginTop: 80 }}>
            <div className="lp-callout reveal">
              <span style={{ fontSize: 20, lineHeight: 1 }}>⚠️</span>
              <div>
                <h3>RepoSage runs third-party code on purpose</h3>
                <p>
                  Executing a stranger's repository means executing a stranger's code.
                  Runs are confined to a throw-away workspace with capped memory, CPU and
                  process count, but this is not a hardened multi-tenant sandbox — run it
                  on infrastructure you are willing to lose, and keep registration closed
                  to people you trust.
                </p>
              </div>
            </div>
          </section>

          {/* ── Closing CTA ───────────────────────────────────────────── */}
          <section className="lp-section reveal" style={{ textAlign: 'center', paddingBottom: 84 }}>
            <h2 className="lp-h2" style={{ marginBottom: 12 }}>
              Point it at something today
            </h2>
            <p className="lp-sec-sub" style={{ maxWidth: 480, margin: '0 auto 26px' }}>
              One prompt is enough to see the whole pipeline work end to end.
            </p>
            <button className="lp-btn lp-btn-primary lp-btn-lg" onClick={() => jumpToAuth('register')}>
              Get started — it's free
            </button>
          </section>

          {/* ── Footer ────────────────────────────────────────────────── */}
          <footer className="lp-footer">
            <Logo size={22} />
            <span style={{ color: 'var(--txt2)', fontWeight: 600 }}>RepoSage</span>
            <span>Autonomous repo exploration &amp; execution</span>
            <div style={{ flex: 1 }} />
            <span>FastAPI · React · MongoDB</span>
          </footer>
        </div>
      </div>
    </div>
  )
}
