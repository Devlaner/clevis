"use client"

import { Button } from "@/components/ui/button"

// Next.js App Router error boundary -- catches any uncaught render-time error below the
// root layout so a crash (e.g. a response-shape mismatch) shows a recoverable fallback
// instead of a blank white screen (issue #370). Not a substitute for TanStack Query's own
// isError handling at the widget level -- this only fires for errors during render itself.
//
// We deliberately don't render error.message: in a production build Next.js replaces it
// with a long generic disclaimer for Server Component errors, and client crashes surface
// raw internals ("Cannot read properties of undefined..."). Neither is useful to a user.
// error.digest (when present) is the reference a maintainer can grep the server logs for.
export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4">
      <div className="card max-w-md w-full px-6 py-8 flex flex-col items-center gap-3 text-center">
        <p className="text-sm font-medium text-foreground">Something went wrong</p>
        <p className="text-sm text-muted-foreground">
          An unexpected error occurred while rendering this page. Try again, or reload if it persists.
        </p>
        {error.digest && (
          <p className="text-xs text-muted-foreground/70 font-mono">Reference: {error.digest}</p>
        )}
        <Button size="sm" variant="outline" onClick={reset} className="mt-2">
          Try again
        </Button>
      </div>
    </div>
  )
}
