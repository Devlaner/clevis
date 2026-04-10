import { PageHeader } from "@/components/page-header"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  FolderGit2,
  GitPullRequest,
  ShieldCheck,
  Users,
  ArrowRight,
  Radio,
} from "lucide-react"
import { Button } from "@/components/ui/button"

const stats = [
  { label: "Repositories", value: "\u2014", icon: FolderGit2 },
  { label: "Open PRs", value: "\u2014", icon: GitPullRequest },
  { label: "Security Score", value: "\u2014", icon: ShieldCheck },
  { label: "Team Members", value: "\u2014", icon: Users },
]

export default function CockpitPage() {
  return (
    <>
      <PageHeader
        title="Cockpit"
        description="Your GitHub organization at a glance."
      />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map((s) => (
          <Card key={s.label} className="group glow-border transition-all hover:glow-sm">
            <CardContent className="flex items-center gap-4 pt-0">
              <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10">
                <s.icon className="size-5 text-primary" />
              </div>
              <div>
                <p className="text-xs text-muted-foreground">{s.label}</p>
                <p className="text-2xl font-bold tracking-tight">{s.value}</p>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="mt-8 grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2 glow-border">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Radio className="size-4 text-primary" />
              Recent Activity
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <div className="flex size-12 items-center justify-center rounded-full bg-muted/50 mb-3">
                <Radio className="size-5 text-muted-foreground" />
              </div>
              <p className="text-sm font-medium text-muted-foreground">
                No activity yet
              </p>
              <p className="mt-1 text-xs text-muted-foreground/60">
                Connect a GitHub account to see your feed
              </p>
            </div>
          </CardContent>
        </Card>

        <Card className="glow-border">
          <CardHeader>
            <CardTitle className="text-base">Quick Actions</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2">
            <Button variant="outline" className="justify-between" render={<a href="/security" />}>
              Run Security Scan
              <ArrowRight className="size-3.5 text-muted-foreground" />
            </Button>
            <Button variant="outline" className="justify-between" render={<a href="/repos" />}>
              Manage Caches
              <ArrowRight className="size-3.5 text-muted-foreground" />
            </Button>
            <Button variant="outline" className="justify-between" render={<a href="/collaborators" />}>
              View Collaborators
              <ArrowRight className="size-3.5 text-muted-foreground" />
            </Button>
          </CardContent>
        </Card>
      </div>
    </>
  )
}
