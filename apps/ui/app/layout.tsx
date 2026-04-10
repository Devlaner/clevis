import "@/app/globals.css"
import { Geist } from "next/font/google"
import { cn } from "@/lib/utils"
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar"
import { TooltipProvider } from "@/components/ui/tooltip"
import { AppSidebar } from "@/components/app-sidebar"
import { Separator } from "@/components/ui/separator"

const geist = Geist({ subsets: ["latin"], variable: "--font-sans" })

export const metadata = {
  title: "clevis",
  description: "GitHub security analytics and cache management",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={cn("dark font-sans", geist.variable)}>
      <body className="min-h-screen bg-grid">
        <TooltipProvider>
          <SidebarProvider>
            <AppSidebar />
            <SidebarInset>
              <header className="flex h-14 items-center gap-2 border-b border-border/50 px-6 backdrop-blur-sm bg-background/80">
                <SidebarTrigger className="-ml-2" />
                <Separator orientation="vertical" className="h-4" />
                <span className="text-sm font-medium text-primary">clevis</span>
              </header>
              <main className="flex-1 p-6">
                {children}
              </main>
            </SidebarInset>
          </SidebarProvider>
        </TooltipProvider>
      </body>
    </html>
  )
}
