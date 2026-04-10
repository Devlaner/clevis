"use client"

import { useState } from "react"
import { PageHeader } from "@/components/page-header"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Shield, AlertTriangle, CheckCircle, XCircle } from "lucide-react"

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080"

export default function SecurityPage() {
  const [owner, setOwner] = useState("")
  const [token, setToken] = useState("")
  const [data, setData] = useState<any>(null)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)

  async function runScan() {
    setError("")
    setLoading(true)
    try {
      const res = await fetch(`${API}/analytics/overview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, token }),
      })
      const json = await res.json()
      if (!res.ok) {
        setError(json.detail || "Scan failed")
        return
      }
      setData(json)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Health & Security"
        description="Run security checks for a GitHub organization and inspect its posture."
      />

      <div className="grid gap-6 md:grid-cols-2">
        <Card className="glow-border">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Shield className="size-5 text-primary" />
              Run Scan
            </CardTitle>
            <CardDescription>
              Enter your organization login and a GitHub token to analyze security posture.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <Input
              placeholder="Organization login"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
            />
            <Input
              placeholder="GitHub token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
            <Button onClick={runScan} disabled={loading || !owner || !token}>
              {loading ? "Scanning..." : "Run analytics"}
            </Button>
            {error && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertTriangle className="size-4" />
                {error}
              </div>
            )}
          </CardContent>
        </Card>

        {data && (
          <Card className="glow-border">
            <CardHeader>
              <CardTitle>Score</CardTitle>
              <CardDescription>
                {data.failed_checks} of {data.total_checks} checks failed
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-center">
                <span className={`text-6xl font-bold ${data.score >= 80 ? "text-accent" : data.score >= 50 ? "text-chart-4" : "text-destructive"}`}>
                  {data.score}
                </span>
                <span className="ml-1 text-2xl text-muted-foreground">/100</span>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {data && (
        <Card className="mt-6 glow-border">
          <CardHeader>
            <CardTitle>Check Results</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="divide-y divide-border/50">
              {data.checks.map((c: any) => (
                <div key={c.id} className="flex items-start gap-4 py-4 first:pt-0 last:pb-0">
                  {c.status === "passed" ? (
                    <CheckCircle className="mt-0.5 size-5 shrink-0 text-accent" />
                  ) : (
                    <XCircle className="mt-0.5 size-5 shrink-0 text-destructive" />
                  )}
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{c.title}</span>
                      <Badge variant={c.severity === "high" ? "destructive" : "secondary"}>
                        {c.severity}
                      </Badge>
                    </div>
                    <p className="text-sm text-muted-foreground">{c.remediation}</p>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </>
  )
}
