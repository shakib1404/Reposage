import { useState, useEffect } from 'react'
import { authRegister, authLogin, authForgot, authReset } from '../api'

export default function AuthPage({ onAuth }) {
  // Check URL for reset token
  const params     = new URLSearchParams(window.location.search)
  const resetToken = params.get('reset_token')

  const [mode,    setMode]    = useState(resetToken ? 'reset' : 'login')
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState('')
  const [success, setSuccess] = useState('')

  // Form fields
  const [username, setUsername] = useState('')
  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [confirm,  setConfirm]  = useState('')

  const reset = () => { setError(''); setSuccess('') }

  const submit = async e => {
    e.preventDefault()
    reset()
    setLoading(true)
    try {
      if (mode === 'register') {
        if (password !== confirm) throw new Error('Passwords do not match')
        const data = await authRegister(username, email, password)
        onAuth(data.token, data.user)

      } else if (mode === 'login') {
        const data = await authLogin(email, password)
        onAuth(data.token, data.user)

      } else if (mode === 'forgot') {
        await authForgot(email)
        setSuccess('Reset link sent! Check your inbox (and spam folder).')

      } else if (mode === 'reset') {
        if (password !== confirm) throw new Error('Passwords do not match')
        await authReset(resetToken, password)
        setSuccess('Password updated! You can now log in.')
        // Clean URL
        window.history.replaceState({}, '', window.location.pathname)
        setTimeout(() => setMode('login'), 1500)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--bg)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 24,
    }}>
      <div style={{
        width: '100%',
        maxWidth: 400,
        background: 'var(--bg2)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        padding: '32px 28px',
      }} className="fade-in">

        {/* Logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 28 }}>
          <div style={{
            width: 34, height: 34, borderRadius: 9,
            background: 'var(--accent)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
            </svg>
          </div>
          <span style={{ fontWeight: 700, fontSize: 17, letterSpacing: '-0.02em' }}>RepoSage</span>
        </div>

        {/* Mode tabs (only for login/register) */}
        {(mode === 'login' || mode === 'register') && (
          <div style={{
            display: 'flex', gap: 0,
            background: 'var(--bg3)',
            borderRadius: 8, padding: 3, marginBottom: 24,
          }}>
            {[['login', 'Sign in'], ['register', 'Create account']].map(([m, label]) => (
              <button
                key={m}
                onClick={() => { setMode(m); reset() }}
                style={{
                  flex: 1, padding: '7px 0', fontSize: 13, fontWeight: 500,
                  border: 'none', borderRadius: 6, cursor: 'pointer',
                  background: mode === m ? 'var(--bg2)' : 'transparent',
                  color: mode === m ? 'var(--txt)' : 'var(--txt2)',
                  boxShadow: mode === m ? '0 1px 3px rgba(0,0,0,0.3)' : 'none',
                  transition: 'all 0.15s',
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        {/* Title for forgot/reset modes */}
        {mode === 'forgot' && (
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Forgot password</h2>
        )}
        {mode === 'reset' && (
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Set new password</h2>
        )}

        {/* Error / success */}
        {error && (
          <div style={{
            background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.4)',
            borderRadius: 6, padding: '9px 12px',
            fontSize: 13, color: '#f87171', marginBottom: 16,
          }}>
            {error}
          </div>
        )}
        {success && (
          <div style={{
            background: 'rgba(52,211,153,0.1)', border: '1px solid rgba(52,211,153,0.4)',
            borderRadius: 6, padding: '9px 12px',
            fontSize: 13, color: '#34d399', marginBottom: 16,
          }}>
            {success}
          </div>
        )}

        {/* Form */}
        <form onSubmit={submit}>
          {mode === 'register' && (
            <Field label="Username" type="text" value={username}
              onChange={e => setUsername(e.target.value)}
              placeholder="yourname" required />
          )}

          {(mode === 'login' || mode === 'register' || mode === 'forgot') && (
            <Field label="Email" type="email" value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@example.com" required />
          )}

          {(mode === 'login' || mode === 'register' || mode === 'reset') && (
            <Field
              label={mode === 'reset' ? 'New password' : 'Password'}
              type="password" value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="••••••••" required />
          )}

          {(mode === 'register' || mode === 'reset') && (
            <Field label="Confirm password" type="password" value={confirm}
              onChange={e => setConfirm(e.target.value)}
              placeholder="••••••••" required />
          )}

          <button
            type="submit"
            disabled={loading}
            style={{
              width: '100%', padding: '11px 0', marginTop: 4,
              background: 'var(--accent)', color: 'white',
              border: 'none', borderRadius: 8, fontSize: 14,
              fontWeight: 600, cursor: loading ? 'not-allowed' : 'pointer',
              opacity: loading ? 0.7 : 1, transition: 'opacity 0.15s',
            }}
          >
            {loading ? (
              <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                <Spinner /> {mode === 'login' ? 'Signing in…' : mode === 'register' ? 'Creating account…' : mode === 'forgot' ? 'Sending…' : 'Updating…'}
              </span>
            ) : (
              mode === 'login' ? 'Sign in' :
              mode === 'register' ? 'Create account' :
              mode === 'forgot' ? 'Send reset link' :
              'Set new password'
            )}
          </button>
        </form>

        {/* Footer links */}
        <div style={{ marginTop: 18, textAlign: 'center', fontSize: 13, color: 'var(--txt2)' }}>
          {mode === 'login' && (
            <button onClick={() => { setMode('forgot'); reset() }}
              style={linkStyle}>
              Forgot password?
            </button>
          )}
          {(mode === 'forgot' || mode === 'reset') && (
            <button onClick={() => { setMode('login'); reset() }}
              style={linkStyle}>
              ← Back to sign in
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function Field({ label, type, value, onChange, placeholder, required }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--txt2)', marginBottom: 6 }}>
        {label}
      </label>
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        required={required}
        style={{
          width: '100%', padding: '9px 12px', boxSizing: 'border-box',
          border: '1px solid var(--border2)',
          borderRadius: 7, background: 'var(--bg3)',
          color: 'var(--txt)', fontFamily: 'var(--font)', fontSize: 14,
          outline: 'none', transition: 'border-color 0.15s',
        }}
        onFocus={e  => e.target.style.borderColor = 'var(--accent)'}
        onBlur={e   => e.target.style.borderColor = 'var(--border2)'}
      />
    </div>
  )
}

const linkStyle = {
  background: 'none', border: 'none', color: 'var(--accent)',
  cursor: 'pointer', fontSize: 13, padding: 0,
  textDecoration: 'underline', textDecorationColor: 'transparent',
}

const Spinner = () => (
  <span style={{
    display: 'inline-block', width: 13, height: 13,
    border: '2px solid rgba(255,255,255,0.3)',
    borderTopColor: 'white', borderRadius: '50%',
    animation: 'spin 0.6s linear infinite',
  }} />
)
