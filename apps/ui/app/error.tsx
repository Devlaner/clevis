"use client"

import { Button } from "@/components/ui/button"

// Next.js App Router error boundary -- catches any uncaught render-time error below the
// root layout so a crash (e.g. a response-shape mismatch) shows a recoverable fallback
// instead of a blank white screen (issue #370). Not a substitute for TanStack Query's own
// isError handling at the widget level -- this only fires for errors during render itself.
export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4">
      <div className="card max-w-md w-full px-6 py-8 flex flex-col items-center gap-3 text-center">
        <p className="text-sm font-medium text-foreground">Something went wrong</p>
        <p className="text-sm text-muted-foreground">
          {error.message || "An unexpected error occurred while rendering this page."}
        </p>
        <Button size="sm" variant="outline" onClick={reset} className="mt-2">
          Try again
        </Button>
      </div>
    </div>
  )
}
