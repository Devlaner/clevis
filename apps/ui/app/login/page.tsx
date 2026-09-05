"use client"

import { useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { useAuth } from "@/lib/auth-context"
import { api } from "@/lib/api/client"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { CircleNotch, Warning } from "@phosphor-icons/react"

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080"

// Inline GitHub logo — keeps the mark on-brand and avoids depending on an icon set's brand glyphs.
function GithubMark() {
  return (
    <svg viewBox="0 0 16 16" className="size-3.5" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
    </svg>
  )
}

// Only accept unambiguous same-site relative paths: reject protocol-relative ("//..."),
// backslash variants (some browsers normalize "/\..." to "//..."), and control characters
// that could let URL parsing reinterpret this as an external navigation target.
function safeNextPath(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "/"
  if (next.includes("\\") || /[\x00-\x1f\x7f]/.test(next)) return "/"
  return next
}

const GITHUB_ERROR_MESSAGES: Record<string, string> = {
  github_not_configured: "GitHub sign-in isn't set up on this server yet. Use email and password, or ask an admin to configure it.",
  github_invalid_state: "Your GitHub sign-in attempt expired or was invalid. Please try again.",
  github_oauth_failed: "GitHub sign-in failed. Please try again.",
  github_email_registered: "An account already exists with this email. Sign in with your password below.",
}

function githubErrorMessage(code: string | null): string {
  if (!code) return ""
  return GITHUB_ERROR_MESSAGES[code] || "GitHub sign-in failed. Please try again."
}

export default function LoginPage() {
  const { user, login, isLoading, logoutWarning, clearLogoutWarning } = useAuth()
  const router = useRouter()
  const searchParams = useSearchParams()
  const next = safeNextPath(searchParams.get("next"))

  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState(() => githubErrorMessage(searchParams.get("error")))
  const [isSubmitting, setIsSubmitting] = useState(false)

  // Strip ?error= from the URL once read, so a refresh doesn't re-show a stale message.
  useEffect(() => {
    if (!searchParams.get("error")) return
    const params = new URLSearchParams(searchParams)
    params.delete("error")
    const qs = params.toString()
    router.replace(qs ? `/login?${qs}` : "/login")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Redirect if already authenticated or setup is needed
  useEffect(() => {
    if (isLoading) return
    if (user) {
      router.replace(next)
      return
    }
    api.auth.setupRequired()
      .then(({ setup_required }) => {
        if (setup_required) router.replace("/setup")
      })
      .catch(() => {
        // API unreachable — stay on login page; user will see the form
      })
  }, [isLoading, user, router, next])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    clearLogoutWarning()
    setIsSubmitting(true)
    try {
      await login(email, password)
      router.replace(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed")
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8">
          <p className="text-[0.6875rem] font-medium text-muted-foreground uppercase tracking-[0.12em] mb-1">
            clevis
          </p>
          <h1 className="text-2xl font-semibold text-foreground">Sign in</h1>
        </div>

        {logoutWarning && (
          <div
            role="alert"
            className="mb-4 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
          >
            <Warning className="size-3.5 mt-0.5 shrink-0" />
            <p>{logoutWarning}</p>
          </div>
        )}

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <Field>
            <FieldLabel>Email</FieldLabel>
            <Input
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </Field>
          <Field>
            <FieldLabel>Password</FieldLabel>
            <Input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </Field>

          {error && (
            <p className="text-xs text-destructive">{error}</p>
          )}

          <Button type="submit" disabled={isSubmitting} className="mt-2">
            {isSubmitting ? (
              <><CircleNotch className="size-3.5 animate-spin" />Signing in…</>
            ) : (
              "Sign in"
            )}
          </Button>
        </form>

        <div className="my-4 flex items-center gap-3">
          <span className="h-px flex-1 bg-border" />
          <span className="text-[0.625rem] uppercase tracking-wider text-muted-foreground">or</span>
          <span className="h-px flex-1 bg-border" />
        </div>

        <Button
          type="button"
          variant="outline"
          className="w-full"
          onClick={() => {
            const qs = next !== "/" ? `?next=${encodeURIComponent(next)}` : ""
            window.location.href = `${API_BASE}/auth/github/login${qs}`
          }}
        >
          <GithubMark />
          Sign in with GitHub
        </Button>

        <p className="text-xs text-muted-foreground mt-4 text-center">
          Don&apos;t have an account?{" "}
          <a href="/register" className="text-foreground underline underline-offset-2">
            Sign up
          </a>
        </p>
      </div>
    </div>
  )
}
