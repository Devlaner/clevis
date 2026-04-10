"use client"

import { usePathname } from "next/navigation"
import {
  LayoutDashboard,
  Activity,
  FolderGit2,
  ShieldCheck,
  Users,
  Zap,
  Building2,
} from "lucide-react"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"

const navItems = [
  { title: "Cockpit", href: "/", icon: LayoutDashboard },
  { title: "Activity", href: "/activity", icon: Activity },
  { title: "Repositories", href: "/repos", icon: FolderGit2 },
  { title: "Health & Security", href: "/security", icon: ShieldCheck },
  { title: "Collaborators", href: "/collaborators", icon: Users },
  { title: "Automation", href: "/automation", icon: Zap },
]

export function AppSidebar() {
  const pathname = usePathname()

  function isActive(href: string) {
    if (href === "/") return pathname === "/"
    return pathname === href || pathname.startsWith(href + "/")
  }

  return (
    <Sidebar>
      <SidebarHeader className="border-b border-border/50 px-6 py-5">
        <div className="flex items-center gap-2">
          <div className="flex size-8 items-center justify-center rounded-lg bg-primary/10 glow-sm">
            <ShieldCheck className="size-4 text-primary" />
          </div>
          <div>
            <span className="text-lg font-bold tracking-tight text-gradient">clevis</span>
            <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
              GitHub Platform
            </p>
          </div>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel className="text-[10px] uppercase tracking-widest text-muted-foreground/60">
            Navigation
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {navItems.map((item) => (
                <SidebarMenuItem key={item.title}>
                  <SidebarMenuButton
                    isActive={isActive(item.href)}
                    className={isActive(item.href) ? "text-primary bg-primary/5 border-l-2 border-primary glow-sm" : ""}
                    render={<a href={item.href} />}
                  >
                    <item.icon className="size-4" />
                    <span>{item.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-t border-border/50 px-4 py-3">
        <div className="flex items-center gap-2 rounded-lg bg-muted/50 px-3 py-2">
          <Building2 className="size-4 text-muted-foreground" />
          <div className="flex-1 truncate">
            <p className="text-xs font-medium text-muted-foreground">No context</p>
            <p className="text-[10px] text-muted-foreground/60">Connect GitHub to begin</p>
          </div>
        </div>
      </SidebarFooter>
    </Sidebar>
  )
}
