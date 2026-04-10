import { PageHeader } from "@/components/page-header"
import { Card, CardContent } from "@/components/ui/card"
import { Users } from "lucide-react"

export default function CollaboratorsPage() {
  return (
    <>
      <PageHeader
        title="Collaborators"
        description="Manage your organization's team and invitations."
      />
      <Card className="glow-border">
        <CardContent>
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-primary/10 mb-3">
              <Users className="size-5 text-primary" />
            </div>
            <p className="text-sm font-medium">Collaborators coming soon</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Team roster, pending invites, and permission management will appear here.
            </p>
          </div>
        </CardContent>
      </Card>
    </>
  )
}
