"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import { useAuth } from "@/lib/auth-context"
import { api } from "@/lib/api/client"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { CircleNotch } from "@phosphor-icons/react"

export default function RegisterPage() {
  const { user, setSession, isLoading } = useAuth()
  const router = useRouter()

  const [name, setName] = useState("")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [confirm, setConfirm] = useState("")
  const [error, setError] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)

  useEffect(() => {
    if (isLoading) return
    if (user) router.replace("/")
  }, [isLoading, user, router])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError("")

    if (password.length < 12) {
      setError("Password must be at least 12 characters.")
      return
    }
    if (password !== confirm) {
      setError("Passwords do not match.")
      return
    }

    setIsSubmitting(true)
    try {
      const { access_token, user: newUser, pending_invitations } = await api.auth.register(
        email,
        password,
        name || undefined,
      )
      setSession(access_token, newUser, pending_invitations)
      router.replace("/")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Registration failed")
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
          <h1 className="text-2xl font-semibold text-foreground">Create an account</h1>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <Field>
            <FieldLabel>Name (optional)</FieldLabel>
            <Input
              placeholder="Your name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
            />
          </Field>
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
              placeholder="At least 12 characters"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="new-password"
            />
          </Field>
          <Field>
            <FieldLabel>Confirm password</FieldLabel>
            <Input
              type="password"
              placeholder="Repeat password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              autoComplete="new-password"
            />
          </Field>

          {error && (
            <p className="text-xs text-destructive">{error}</p>
          )}

          <Button type="submit" disabled={isSubmitting} className="mt-2">
            {isSubmitting ? (
              <><CircleNotch className="size-3.5 animate-spin" />Creating account…</>
            ) : (
              "Create account →"
            )}
          </Button>
        </form>

        <p className="text-xs text-muted-foreground mt-4 text-center">
          Already have an account?{" "}
          <a href="/login" className="text-foreground underline underline-offset-2">
            Sign in
          </a>
        </p>
      </div>
    </div>
  )
}
