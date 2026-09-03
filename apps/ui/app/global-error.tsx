"use client"

// Last-resort error boundary for issue #370. A Next.js App Router `error.tsx` boundary
// wraps the *page* below the root layout but NOT the root `layout.tsx` / `template.tsx`
// themselves -- if the layout throws during render, `error.tsx` can't catch it and the
// user is back to a blank white screen. `global-error.tsx` is the only boundary that
// covers a root-layout crash; when it fires it *replaces* the root layout, so it must
// render its own `<html>` / `<body>`, and it does not inherit `globals.css`, fonts, or
// providers -- hence the inline styles (a broken layout is exactly when a dark ground and
// legible text matter most).
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "1rem",
          background: "#0a0a0b",
          color: "#e5e5e5",
          fontFamily:
            "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
        }}
      >
        <div style={{ maxWidth: "28rem", width: "100%", textAlign: "center" }}>
          <p style={{ fontSize: "0.875rem", fontWeight: 500, margin: "0 0 0.5rem" }}>
            Something went wrong
          </p>
          <p style={{ fontSize: "0.875rem", color: "#a1a1aa", margin: "0 0 1rem" }}>
            The app failed to load. Try again, or reload the page if it persists.
          </p>
          {error.digest && (
            <p
              style={{
                fontSize: "0.75rem",
                color: "#71717a",
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                margin: "0 0 1rem",
              }}
            >
              Reference: {error.digest}
            </p>
          )}
          <button
            onClick={reset}
            style={{
              fontSize: "0.8125rem",
              padding: "0.375rem 0.875rem",
              borderRadius: "0.375rem",
              border: "1px solid #3f3f46",
              background: "transparent",
              color: "inherit",
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  )
}
