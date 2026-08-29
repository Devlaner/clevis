"use client"

import { useEffect, useState } from "react"

// Theme system — neutral "shade/vibe" palettes (whites/blacks and muted tints).
// Colors are driven entirely by CSS variables in globals.css, keyed by the
// `data-theme` attribute on <html>. The dark/light class is toggled alongside so
// the few `dark:` Tailwind utilities (badge, input) keep working.

export type ThemeName = "midnight" | "carbon" | "slate" | "dim" | "paper" | "ash"

export interface ThemeMeta {
  name: ThemeName
  label: string
  isDark: boolean
}

export const THEMES: ThemeMeta[] = [
  { name: "midnight", label: "Ember Terminal", isDark: true },
  { name: "carbon", label: "Carbon", isDark: true },
  { name: "slate", label: "Slate", isDark: true },
  { name: "dim", label: "Dim", isDark: true },
  { name: "paper", label: "Ember Light", isDark: false },
  { name: "ash", label: "Ash", isDark: false },
]

export const DEFAULT_THEME: ThemeName = "midnight"
export const THEME_STORAGE_KEY = "clevis:theme"

function isThemeName(value: string | null): value is ThemeName {
  return !!value && THEMES.some((t) => t.name === value)
}

/** Apply a theme to <html> and persist the choice. Client-side only. */
export function applyTheme(name: ThemeName): void {
  const meta = THEMES.find((t) => t.name === name) ?? THEMES[0]
  const root = document.documentElement
  root.dataset.theme = meta.name
  root.classList.toggle("dark", meta.isDark)
  root.classList.toggle("light", !meta.isDark)
  try {
    localStorage.setItem(THEME_STORAGE_KEY, meta.name)
  } catch {
    // localStorage unavailable (private mode etc.) — theme still applies for this session.
  }
}

/** Read/set the active theme. Reads the persisted value after mount. */
export function useTheme() {
  const [theme, setThemeState] = useState<ThemeName>(DEFAULT_THEME)

  useEffect(() => {
    const stored = localStorage.getItem(THEME_STORAGE_KEY)
    setThemeState(isThemeName(stored) ? stored : DEFAULT_THEME)
  }, [])

  const setTheme = (name: ThemeName) => {
    applyTheme(name)
    setThemeState(name)
  }

  return { theme, setTheme }
}
