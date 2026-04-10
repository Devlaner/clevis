import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Activity } from "lucide-react"

export default function ActivityPage() {
  return (
    <>
      <PageHeader
        title="Activity"
        description="See what's happening across your repositories."
      />
      <Card className="glow-border">
        <CardContent>
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-primary/10 mb-3">
              <Activity className="size-5 text-primary" />
            </div>
            <p className="text-sm font-medium">Activity feed coming soon</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Event timeline, commit digests, and org pulse will appear here.
            </p>
          </div>
        </CardContent>
      </Card>
    </>
  )
}
