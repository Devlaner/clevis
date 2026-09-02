// Minimal RFC-4180 CSV serialiser with spreadsheet formula-injection defense.
// Dependency-free and pure (no DOM) so it unit-tests cleanly and can run
// server- or client-side.

/**
 * Serialise one field:
 * - Fields Excel/Sheets would parse as a formula (leading =, +, -, @, tab, CR)
 *   are prefixed with a single quote so a compliance export can't smuggle a
 *   formula into a reviewer's spreadsheet. A plain number (incl. negative) is
 *   left alone -- it's not a formula.
 * - Then quote if the (possibly prefixed) value contains a comma, quote, CR, or
 *   LF, doubling any embedded quotes (RFC 4180).
 */
function escapeField(value: unknown): string {
  let s = value === null || value === undefined ? "" : String(value)
  if (/^[=+\-@\t\r]/.test(s) && !/^-?\d+(\.\d+)?$/.test(s)) s = `'${s}`
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
