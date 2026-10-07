import { useEffect, useState } from 'react'

const KEY = 'rs_theme'
const EVENT = 'rs-themechange'

export function getTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
}

export function setTheme(theme) {
  const t = theme === 'light' ? 'light' : 'dark'
  document.documentElement.dataset.theme = t
  try { localStorage.setItem(KEY, t) } catch { /* private mode: theme just won't persist */ }
  window.dispatchEvent(new CustomEvent(EVENT, { detail: t }))
}

export function toggleTheme() {
  setTheme(getTheme() === 'light' ? 'dark' : 'light')
}

/** Re-renders the caller whenever the theme changes — needed by anything that
 *  paints with JS (canvas graphs, Mermaid) since CSS variables don't reach it. */
export function useTheme() {
  const [theme, set] = useState(getTheme)
  useEffect(() => {
    const on = e => set(e.detail)
    window.addEventListener(EVENT, on)
    return () => window.removeEventListener(EVENT, on)
  }, [])
  return theme
}

/** Resolve a CSS custom property for canvas drawing. */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** `rgba()` from the theme's ink triplet (`--ink`): white-ish in dark mode,
 *  near-black in light mode — what canvas edges and labels are drawn with. */
export function ink(alpha) {
  return `rgba(${cssVar('--ink') || '255,255,255'},${alpha})`
}
