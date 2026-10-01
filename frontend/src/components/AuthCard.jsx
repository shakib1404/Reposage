/**
 * AuthCard — the sign-in / register / forgot / reset form.
 *
 * Extracted from the old full-screen AuthPage so it can sit inside the
 * landing page's hero: the marketing explanation and the form are on the
 * same screen, no navigation step in between.
 *
 * All four modes live here because they share one submit handler and one
 * error/success surface. `reset` is entered automatically when the browser
 * lands on ?reset_token=… (the link e-mailed by /auth/forgot-password).
 */
import { useState } from 'react'
import { authRegister, authLogin, authForgot, authReset } from '../api'

const TITLES = {
  login:    { h: 'Welcome back',      s: 'Sign in to pick up where you left off.' },
  register: { h: 'Create an account', s: 'Free, and takes about ten seconds.' },
  forgot:   { h: 'Forgot password',   s: "We'll e-mail you a reset link." },
  reset:    { h: 'Set a new password', s: 'Choose something you have not used before.' },
}

const SUBMIT_LABEL = {
  login:    ['Sign in',            'Signing in…'],
  register: ['Create account',     'Creating account…'],
  forgot:   ['Send reset link',    'Sending…'],
  reset:    ['Set new password',   'Updating…'],
}

export default function AuthCard({ onAuth, mode, setMode, resetToken = '' }) {
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState('')
  const [success, setSuccess] = useState('')
  const [showPw,  setShowPw]  = useState(false)

  const [username, setUsername] = useState('')
  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [confirm,  setConfirm]  = useState('')

  const clearMsgs = () => { setError(''); setSuccess('') }
  const switchTo  = m => { setMode(m); clearMsgs() }

  const submit = async e => {
    e.preventDefault()
    clearMsgs()
    setLoading(true)
    try {
      if (mode === 'register') {
        if (password !== confirm)  throw new Error('Passwords do not match')
        if (password.length < 6)   throw new Error('Password must be at least 6 characters')
        const data = await authRegister(username, email, password)
        onAuth(data.token, data.user)

      } else if (mode === 'login') {
        const data = await authLogin(email, password)
        onAuth(data.token, data.user)

      } else if (mode === 'forgot') {
        await authForgot(email)
        setSuccess('Reset link sent — check your inbox, and the spam folder.')

      } else if (mode === 'reset') {
        if (password !== confirm) throw new Error('Passwords do not match')
        if (password.length < 6)  throw new Error('Password must be at least 6 characters')
        await authReset(resetToken, password)
        setSuccess('Password updated. You can sign in now.')
        // Drop the token from the address bar so a refresh doesn't re-enter
        // reset mode with an already-spent token.
        window.history.replaceState({}, '', window.location.pathname)
        setTimeout(() => setMode('login'), 1500)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const t = TITLES[mode]
  const [labelIdle, labelBusy] = SUBMIT_LABEL[mode]

  return (
    <div
      id="auth"
      style={{
        background: 'linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015))',
        border: '1px solid var(--border2)',
        borderRadius: 'var(--radius-lg)',
        padding: '26px 24px',
        boxShadow: 'var(--shadow-lg)',
        backdropFilter: 'blur(10px)',
        WebkitBackdropFilter: 'blur(10px)',
        scrollMarginTop: 80,
      }}
    >
      {/* ── Mode switch ─────────────────────────────────────────────────── */}
      {(mode === 'login' || mode === 'register') && (
        <div style={{
          display: 'flex', background: 'rgba(0,0,0,0.3)',
          border: '1px solid var(--border)',
          borderRadius: 9, padding: 3, marginBottom: 22,
        }}>
          {[['login', 'Sign in'], ['register', 'Create account']].map(([m, label]) => (
            <button
              key={m}
              type="button"
              onClick={() => switchTo(m)}
              aria-pressed={mode === m}
              style={{
                flex: 1, padding: '8px 0', fontSize: 13, fontWeight: 600,
                fontFamily: 'var(--font)',
                border: 'none', borderRadius: 7, cursor: 'pointer',
                background: mode === m ? 'var(--bg3)' : 'transparent',
                color:      mode === m ? 'var(--txt)' : 'var(--txt2)',
                boxShadow:  mode === m ? '0 1px 4px rgba(0,0,0,0.4)' : 'none',
                transition: 'all 0.15s',
              }}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      <h2 style={{ fontSize: 17, fontWeight: 600, letterSpacing: '-0.02em', marginBottom: 5 }}>
        {t.h}
      </h2>
      <p style={{ fontSize: 12.5, color: 'var(--txt2)', marginBottom: 20, lineHeight: 1.5 }}>
        {t.s}
      </p>

      {error && <Banner kind="error">{error}</Banner>}
      {success && <Banner kind="ok">{success}</Banner>}

      <form onSubmit={submit}>
        {mode === 'register' && (
          <Field label="Username" type="text" value={username} autoComplete="username"
            onChange={e => setUsername(e.target.value)} placeholder="yourname" required />
        )}

        {(mode === 'login' || mode === 'register' || mode === 'forgot') && (
          <Field label="Email" type="email" value={email} autoComplete="email"
            onChange={e => setEmail(e.target.value)} placeholder="you@example.com" required />
        )}

        {(mode === 'login' || mode === 'register' || mode === 'reset') && (
          <Field
            label={mode === 'reset' ? 'New password' : 'Password'}
            type={showPw ? 'text' : 'password'}
            value={password}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            onChange={e => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            trailing={
              <button
                type="button"
                onClick={() => setShowPw(v => !v)}
                aria-label={showPw ? 'Hide password' : 'Show password'}
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: 'var(--txt3)', fontSize: 11.5, fontWeight: 500,
                  fontFamily: 'var(--font)', padding: '0 4px',
                }}
              >
                {showPw ? 'Hide' : 'Show'}
              </button>
            }
          />
        )}

        {(mode === 'register' || mode === 'reset') && (
          <Field label="Confirm password" type={showPw ? 'text' : 'password'}
            value={confirm} autoComplete="new-password"
            onChange={e => setConfirm(e.target.value)} placeholder="••••••••" required />
        )}

        <button
          type="submit"
          disabled={loading}
          className="lp-btn lp-btn-primary"
          style={{ width: '100%', padding: '11px 0', marginTop: 6, fontSize: 14 }}
        >
          {loading ? <><Spinner /> {labelBusy}</> : labelIdle}
        </button>
      </form>

      <div style={{ marginTop: 16, textAlign: 'center', fontSize: 12.5 }}>
        {mode === 'login' && (
          <button type="button" onClick={() => switchTo('forgot')} style={linkStyle}>
            Forgot your password?
          </button>
        )}
        {(mode === 'forgot' || mode === 'reset') && (
          <button type="button" onClick={() => switchTo('login')} style={linkStyle}>
            ← Back to sign in
          </button>
        )}
        {mode === 'register' && (
          <span style={{ color: 'var(--txt3)', lineHeight: 1.55 }}>
            Your runs stay private to your account.
          </span>
        )}
      </div>
    </div>
  )
}

function Banner({ kind, children }) {
  const ok = kind === 'ok'
  return (
    <div
      role={ok ? 'status' : 'alert'}
      style={{
        background: ok ? 'var(--green-dim)' : 'var(--red-dim)',
        border: `1px solid ${ok ? 'rgba(52,211,153,0.4)' : 'rgba(248,113,113,0.4)'}`,
        borderRadius: 7, padding: '9px 12px',
        fontSize: 12.5, lineHeight: 1.5,
        color: ok ? 'var(--green)' : 'var(--red)',
        marginBottom: 16,
      }}
    >
      {children}
    </div>
  )
}

function Field({ label, type, value, onChange, placeholder, required, autoComplete, trailing }) {
  const [focus, setFocus] = useState(false)
  return (
    <div style={{ marginBottom: 14 }}>
      <label style={{
        display: 'block', fontSize: 11.5, fontWeight: 600,
        color: 'var(--txt2)', marginBottom: 6, letterSpacing: '0.01em',
      }}>
        {label}
      </label>
      <div style={{
        display: 'flex', alignItems: 'center',
        border: `1px solid ${focus ? 'var(--accent)' : 'var(--border2)'}`,
        borderRadius: 8, background: 'rgba(0,0,0,0.28)',
        boxShadow: focus ? '0 0 0 3px rgba(93,142,255,0.14)' : 'none',
        transition: 'border-color 0.15s, box-shadow 0.15s',
        paddingRight: trailing ? 10 : 0,
      }}>
        <input
          type={type}
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          required={required}
          autoComplete={autoComplete}
          onFocus={() => setFocus(true)}
          onBlur={()  => setFocus(false)}
          style={{
            flex: 1, minWidth: 0,
            padding: '10px 12px', border: 'none', outline: 'none',
            background: 'transparent', color: 'var(--txt)',
            fontFamily: 'var(--font)', fontSize: 13.5,
          }}
        />
        {trailing}
      </div>
    </div>
  )
}

const linkStyle = {
  background: 'none', border: 'none', color: 'var(--accent)',
  cursor: 'pointer', fontSize: 12.5, padding: 0, fontFamily: 'var(--font)',
}

const Spinner = () => (
  <span style={{
    display: 'inline-block', width: 13, height: 13,
    border: '2px solid rgba(255,255,255,0.35)',
    borderTopColor: '#fff', borderRadius: '50%',
    animation: 'spin 0.6s linear infinite',
  }} />
)
