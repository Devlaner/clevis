import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Zap } from "lucide-react"

export default function AutomationPage() {
  return (
    <>
      <PageHeader
        title="Automation"
        description="Manage workflows and automated actions."
      />
      <Card className="glow-border">
        <CardContent>
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-primary/10 mb-3">
              <Zap className="size-5 text-primary" />
            </div>
            <p className="text-sm font-medium">Automation coming soon</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Workflow triggers, guarded dispatches, and repo scaffolding will appear here.
            </p>
          </div>
        </CardContent>
      </Card>
    </>
  )
}
