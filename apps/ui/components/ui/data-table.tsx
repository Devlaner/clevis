"use client"

import { useEffect, useMemo, useState } from "react"
import { CaretUp, CaretDown, CaretUpDown } from "@phosphor-icons/react"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"

export interface DataTableColumn<T> {
  key: string
  header: string
  align?: "left" | "right"
  render: (row: T) => React.ReactNode
  /** Enables click-to-sort on this column's header. Omit for columns that shouldn't sort
   * (actions, badges, etc). */
  sortValue?: (row: T) => string | number
  headerClassName?: string
  cellClassName?: string
}

interface DataTableProps<T> {
  columns: DataTableColumn<T>[]
  data: T[]
  getRowKey: (row: T) => string | number
  /** Rows per page. Pagination controls only render once data exceeds this. */
  pageSize?: number
  rowClassName?: (row: T) => string | undefined
  /** Attaches a ref to a specific row's <tr> -- e.g. to scroll a highlighted row into
   * view. Most callers don't need this. */
  getRowRef?: (row: T) => React.Ref<HTMLTableRowElement> | undefined
}

/** A shared sortable, paginated table -- built on the same raw `<table>` markup/classes
 * every hand-rolled table in this app already uses, so adopting it doesn't introduce a
 * second visual style. Sorting and pagination are both client-side over whatever `data`
 * the caller passes in; for a table backed by a capped/paginated API response (like the
 * audit log), that's the caller's own concern, not this component's. */
export function DataTable<T>({
  columns,
  data,
  getRowKey,
  pageSize = 20,
  rowClassName,
  getRowRef,
}: DataTableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc")
  const [page, setPage] = useState(1)

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sortKey)
    if (!col?.sortValue) return data
    const sortValue = col.sortValue
    return [...data].sort((a, b) => {
      const av = sortValue(a)
      const bv = sortValue(b)
      const cmp = av < bv ? -1 : av > bv ? 1 : 0
      return sortDir === "asc" ? cmp : -cmp
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, sortKey, sortDir])

  // A stale page offset after the underlying data or sort changes would otherwise show
  // an out-of-range (empty-looking) page instead of resetting to the top.
  useEffect(() => {
    setPage(1)
  }, [data, sortKey, sortDir])

  const lastPage = Math.max(1, Math.ceil(sorted.length / pageSize))
  const pageRows = sorted.slice((page - 1) * pageSize, page * pageSize)

  function toggleSort(key: string) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"))
    } else {
      setSortKey(key)
      setSortDir("asc")
    }
  }

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border">
              {columns.map((col) => {
                const isSorted = col.sortValue && sortKey === col.key
                return (
                  <th
                    key={col.key}
                    className={cn(
                      "font-medium px-4 py-2 text-muted-foreground",
                      col.align === "right" ? "text-right" : "text-left",
                      col.sortValue && "cursor-pointer select-none hover:text-foreground",
                      col.headerClassName,
                    )}
                    onClick={col.sortValue ? () => toggleSort(col.key) : undefined}
                    aria-sort={isSorted ? (sortDir === "asc" ? "ascending" : "descending") : undefined}
                  >
                    <span className="inline-flex items-center gap-1">
                      {col.header}
                      {col.sortValue && (
                        isSorted ? (
                          sortDir === "asc" ? <CaretUp className="size-3" /> : <CaretDown className="size-3" />
                        ) : (
                          <CaretUpDown className="size-3 opacity-40" />
                        )
                      )}
                    </span>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {pageRows.map((row) => (
              <tr key={getRowKey(row)} ref={getRowRef?.(row)} className={rowClassName?.(row)}>
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={cn("px-4 py-2.5", col.align === "right" && "text-right", col.cellClassName)}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {sorted.length > pageSize && (
        <div className="px-4 py-3 border-t border-border flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            Page {page} of {lastPage} · {sorted.length} total
          </span>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              Prev
            </Button>
            <Button size="sm" variant="outline" disabled={page >= lastPage} onClick={() => setPage((p) => p + 1)}>
              Next
            </Button>
          </div>
        </div>
      )}
    </>
  )
}
