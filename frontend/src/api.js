const BASE = '/api'

// ── Auth token helpers ────────────────────────────────────────────────────────
export const getToken  = ()      => localStorage.getItem('rm_token') || ''
export const setToken  = token   => localStorage.setItem('rm_token', token)
export const clearToken = ()     => localStorage.removeItem('rm_token')

function authHeaders() {
  const token = getToken()
  return token
    ? { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
    : { 'Content-Type': 'application/json' }
}

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  })
  if (res.status === 401) {
    // Only treat as "session expired" when the user already has a stored token
    // AND this is NOT an auth endpoint (login/register return 401 for bad creds)
    const isAuthEndpoint = url.includes('/auth/login') ||
                           url.includes('/auth/register') ||
                           url.includes('/auth/forgot') ||
                           url.includes('/auth/reset')
    if (!isAuthEndpoint && getToken()) {
      clearToken()
      window.location.reload()
      throw new Error('Session expired — please log in again')
    }
    // For auth endpoints or unauthenticated requests, show the actual error
    let msg = 'Unauthorised'
    try { const d = await res.json(); msg = d.detail || d.message || msg } catch {}
    throw new Error(msg)
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try { const d = await res.json(); msg = d.detail || d.message || msg } catch {}
    throw new Error(msg)
  }
  return res
}


// ═══════════════════════════════════════════════════════════════════════════════
//  Auth API
// ═══════════════════════════════════════════════════════════════════════════════

export async function authRegister(username, email, password) {
  const res = await apiFetch(`${BASE}/auth/register`, {
    method: 'POST',
    body: JSON.stringify({ username, email, password }),
  })
  return res.json()
}

export async function authLogin(email, password) {
  const res = await apiFetch(`${BASE}/auth/login`, {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  return res.json()
}

export async function authForgot(email) {
  const res = await apiFetch(`${BASE}/auth/forgot-password`, {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
  return res.json()
}

export async function authReset(token, new_password) {
  const res = await apiFetch(`${BASE}/auth/reset-password`, {
    method: 'POST',
    body: JSON.stringify({ token, new_password }),
  })
  return res.json()
}

export async function authMe() {
  const res = await apiFetch(`${BASE}/auth/me`)
  return res.json()
}


// ═══════════════════════════════════════════════════════════════════════════════
//  History API
// ═══════════════════════════════════════════════════════════════════════════════

export async function getHistory() {
  const res = await apiFetch(`${BASE}/history`)
  return res.json()
}

export async function getHistoryEntry(id) {
  const res = await apiFetch(`${BASE}/history/${id}`)
  return res.json()
}

export async function createHistory(task, repos) {
  const res = await apiFetch(`${BASE}/history`, {
    method: 'POST',
    body: JSON.stringify({ task, repos }),
  })
  return res.json()   // { history_id }
}

export async function updateHistory(id, fields) {
  if (!id) return
  await apiFetch(`${BASE}/history/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(fields),
  })
}

export async function deleteHistory(id) {
  await apiFetch(`${BASE}/history/${id}`, { method: 'DELETE' })
}


// ═══════════════════════════════════════════════════════════════════════════════
//  Core app API  (same as before but now includes auth headers via apiFetch)
// ═══════════════════════════════════════════════════════════════════════════════

export async function uploadFiles(task, files) {
  const form = new FormData()
  form.append('task', task)
  for (const f of files) form.append('files', f)
  const token = getToken()
  const res = await fetch(`${BASE}/upload`, {
    method: 'POST',
    body: form,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export async function searchRepos(task) {
  const res = await apiFetch(`${BASE}/search`, {
    method: 'POST',
    body: JSON.stringify({ task }),
  })
  return res.json()
}

export async function analyzeRepo(task, repo) {
  const res = await apiFetch(`${BASE}/analyze`, {
    method: 'POST',
    body: JSON.stringify({
      task,
      repo_full_name:   repo.full_name,
      repo_description: repo.description || '',
    }),
  })
  return res.json()
}

/**
 * Stream execution events via SSE.
 */
export function streamExecution(task, repoFullName, analysis, onEvent, jobId = '', inputFiles = []) {
  const controller = new AbortController()
  const token      = getToken()

  fetch(`${BASE}/execute`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ task, repo_full_name: repoFullName, analysis, job_id: jobId, input_files: inputFiles }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok) { onEvent({ type: 'error', title: 'Error', body: await res.text() }); return }
    const reader  = res.body.getReader()
    const decoder = new TextDecoder()
    let   buffer  = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop()
      for (const part of parts) {
        const line = part.trim()
        if (line.startsWith('data: ')) {
          try { onEvent(JSON.parse(line.slice(6))) } catch {}
        }
      }
    }
  }).catch(err => {
    if (err.name !== 'AbortError')
      onEvent({ type: 'error', title: 'Connection error', body: err.message })
  })

  return () => controller.abort()
}

export function streamTest(repoFullName, jobId, onEvent) {
  const controller = new AbortController()
  const token      = getToken()

  fetch(`${BASE}/test`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ repo_full_name: repoFullName, job_id: jobId || '' }),
    signal: controller.signal,
  }).then(async res => {
    if (!res.ok) { onEvent({ type: 'error', title: 'Error', body: await res.text() }); return }
    const reader  = res.body.getReader()
    const decoder = new TextDecoder()
    let   buffer  = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop()
      for (const part of parts) {
        const line = part.trim()
        if (line.startsWith('data: ')) {
          try { onEvent(JSON.parse(line.slice(6))) } catch {}
        }
      }
    }
  }).catch(err => {
    if (err.name !== 'AbortError')
      onEvent({ type: 'error', title: 'Connection error', body: err.message })
  })

  return () => controller.abort()
}

export async function submitCredentials(jobId, credentials) {
  const res = await apiFetch(`${BASE}/credentials/${jobId}`, {
    method: 'POST',
    body: JSON.stringify({ credentials }),
  })
  return res.json()
}

export async function listOutputs(jobId) {
  const res = await apiFetch(`${BASE}/outputs/${jobId}`)
  return res.json()
}

export function getOutputUrl(jobId, filename) {
  return `${BASE}/outputs/${jobId}/${encodeURIComponent(filename)}`
}

export async function deleteOutputs(jobId) {
  const res = await apiFetch(`${BASE}/outputs/${jobId}`, { method: 'DELETE' })
  return res.json()
}


