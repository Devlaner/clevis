"use client"

import { useEffect, useRef, useState } from "react"
import { useSearchParams } from "next/navigation"
import { PageHeader } from "@/components/page-header"
import { api } from "@/lib/api/client"
import { CheckCircle, Warning, CircleNotch } from "@phosphor-icons/react"

export default function VerifyEmailPage() {
  const searchParams = useSearchParams()
  const token = searchParams.get("token")
  const [state, setState] = useState<"pending" | "success" | "error">("pending")
  const [errorMessage, setErrorMessage] = useState("")
  // Tracks which token value we've already POSTed. Scoped to the token, not the
  // component instance: a boolean "ran" flag would also swallow a genuinely new
  // token (URL changes, or a missing token becoming present) after the first run.
  const attemptedToken = useRef<string | null>(null)

  useEffect(() => {
    if (!token) {
      setState("error")
      setErrorMessage("This verification link is missing its token.")
      return
    }
    // The verification token is single-use: React Strict Mode (dev) double-invokes
    // this effect. Without this guard the second POST 400s on the now-consumed token
    // and overwrites a real "success" with an error. A hard remount (new component
    // instance) still can't be deduped from the client alone -- the API clears the
    // token server-side, so a resubmit legitimately surfaces "expired".
    if (attemptedToken.current === token) return
    attemptedToken.current = token
    setState("pending")
    api.auth
      .verifyEmail(token)
      .then(() => setState("success"))
      .catch((err) => {
        setState("error")
        setErrorMessage(err instanceof Error ? err.message : "Verification failed")
      })
  }, [token])

  return (
    <div className="max-w-md mx-auto mt-16">
      <PageHeader title="Verify your email" description="Confirming your Clevis account email address." />

      <div className="card">
        <div className="p-4 flex flex-col gap-3">
          {state === "pending" ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <CircleNotch className="size-3.5 animate-spin" /> Verifying…
            </div>
          ) : state === "success" ? (
            <p className="text-sm text-primary flex items-center gap-1.5">
              <CheckCircle className="size-3.5" /> Your email is verified. You can now accept organization
              invitations.
            </p>
          ) : (
            <p className="text-sm text-destructive flex items-center gap-1.5">
              <Warning className="size-3.5" /> {errorMessage}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
