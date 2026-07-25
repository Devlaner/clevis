"use client"

import { usePathname } from "next/navigation"

const ROUTE_LABELS: Record<string, string> = {
  "/":              "overview",
  "/activity":      "activity",
  "/repos":         "repositories",
  "/security":      "health & security",
  "/collaborators": "collaborators",
  "/automation":    "automation",
  "/pulls":         "pull requests",
  "/releases":      "releases",
  "/audit":         "audit log",
  "/settings":      "settings",
  "/my/prs":        "my prs",
  "/my/reviews":    "my reviews",
  "/my/issues":     "my issues",
}

function segmentsFrom(pathname: string): string[] {
  // /repos/acme~api/cache → ["repositories", "acme/api", "cache"]
  const parts = pathname.split("/").filter(Boolean)
  if (parts.length === 0) return ["overview"]

  // Try exact match first
  const exact = ROUTE_LABELS[pathname]
  if (exact) return [exact]

  return parts.map((part, i) => {
    const prefix = "/" + parts.slice(0, i + 1).join("/")
    return ROUTE_LABELS[prefix] ?? part.replace("~", "/")
  })
}

export function Breadcrumb() {
  const pathname = usePathname()
  const segments = segmentsFrom(pathname)

  return (
    <nav aria-label="breadcrumb">
      <ol className="flex items-center gap-1.5 text-[0.75rem] text-muted-foreground">
        <li className="text-subtle-foreground">clevis</li>
        {segments.map((seg, i) => (
          <li key={i} className="flex items-center gap-1.5">
            <span className="text-subtle-foreground opacity-40">/</span>
            <span className={i === segments.length - 1 ? "text-muted-foreground" : "text-subtle-foreground"}>
              {seg}
            </span>
          </li>
        ))}
      </ol>
    </nav>
  )
}
