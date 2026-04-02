"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"
import { PageHeader } from "@/components/page-header"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Database } from "lucide-react"

export default function ReposPage() {
  const router = useRouter()
  const [owner, setOwner] = useState("")
  const [repo, setRepo] = useState("")

  function navigate() {
    if (owner && repo) {
      router.push(`/repos/${owner}~${repo}/cache`)
    }
  }

  return (
    <>
      <PageHeader
        title="Cache Management"
        description="Manage GitHub Actions caches for your repositories."
      />

      <Card className="max-w-lg">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="size-5" />
            Select Repository
          </CardTitle>
          <CardDescription>
            Enter the owner and repository name to manage its Actions cache.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <Input
            placeholder="Owner (e.g. octocat)"
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
          />
          <Input
            placeholder="Repository (e.g. hello-world)"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
          <Button onClick={navigate} disabled={!owner || !repo}>
            Open cache manager
          </Button>
        </CardContent>
      </Card>
    </>
  )
}
