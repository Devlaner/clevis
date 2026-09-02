// Minimal RFC-4180 CSV serialiser. Kept dependency-free and pure (no DOM) so it
// unit-tests cleanly and can run server- or client-side.

/** Quote a single field if it contains a comma, quote, CR, or LF; double any embedded quotes. */
function escapeField(value: unknown): string {
  const s = value === null || value === undefined ? "" : String(value)
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export interface CsvColumn<T> {
  header: string
  value: (row: T) => unknown
}

/** Render `rows` as a CSV string with a header line. Uses CRLF line endings per RFC 4180. */
export function toCsv<T>(rows: readonly T[], columns: readonly CsvColumn<T>[]): string {
  const lines = [columns.map((c) => escapeField(c.header)).join(",")]
  for (const row of rows) {
    lines.push(columns.map((c) => escapeField(c.value(row))).join(","))
  }
  return lines.join("\r\n")
}
