// Triggers a browser "save file" for an in-memory string. Isolated here so
// components don't touch Blob/URL plumbing directly and so it can be stubbed in
// tests. No-op on the server (no `document`).

export function downloadTextFile(filename: string, content: string, mimeType = "text/plain"): void {
  if (typeof document === "undefined") return
  const blob = new Blob([content], { type: `${mimeType};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoke on the next tick so the click has been dispatched first.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
