"use client"

import { LayoutDashboard, List, Settings, ChevronLeft, ChevronRight, Mic2 } from "lucide-react"
import { cn } from "@/lib/utils"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { ShieldCheck } from "lucide-react"

type View = "dashboard" | "leads" | "settings"

interface AppSidebarProps {
  currentView: View
  onViewChange: (view: View) => void
  collapsed: boolean
  onToggleCollapse: () => void
}

export function AppSidebar({ currentView, onViewChange, collapsed, onToggleCollapse }: AppSidebarProps) {
  const navItems = [
    { id: "dashboard" as const, label: "Dashboard", icon: LayoutDashboard },
    { id: "leads" as const, label: "All Leads", icon: List },
    { id: "settings" as const, label: "Settings", icon: Settings },
    { id: "admin" as const, label: "Admin", icon: ShieldCheck, view: "admin" },
  ]

  return (
    <aside
      className={cn(
        "flex flex-col bg-sidebar text-sidebar-foreground h-screen transition-all duration-300 relative",
        collapsed ? "w-16" : "w-60"
      )}
    >
      {/* Collapse toggle */}
      <Button
        variant="ghost"
        size="icon"
        onClick={onToggleCollapse}
        className="absolute -right-3 top-6 z-10 size-6 rounded-full border border-sidebar-border bg-sidebar text-sidebar-foreground hover:bg-sidebar-accent"
      >
        {collapsed ? <ChevronRight className="size-3" /> : <ChevronLeft className="size-3" />}
      </Button>

      {/* Logo */}
      <div className={cn("flex items-center gap-2 p-4 border-b border-sidebar-border", collapsed && "justify-center")}>
        <div className="flex size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
          <Mic2 className="size-4" />
        </div>
        {!collapsed && (
          <div className="flex flex-col">
            <span className="text-sm font-semibold">SpeakerAgent</span>
            <span className="text-xs text-sidebar-muted">.AI</span>
          </div>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-2 space-y-1">
        {navItems.map((item) => (
          <button
            key={item.id}
            onClick={() => onViewChange(item.id)}
            className={cn(
              "flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors",
              currentView === item.id
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-sidebar-muted hover:bg-sidebar-accent/50 hover:text-sidebar-foreground",
              collapsed && "justify-center px-2"
            )}
          >
            <item.icon className="size-4 shrink-0" />
            {!collapsed && <span>{item.label}</span>}
          </button>
        ))}
      </nav>

      {/* User section */}
      <div className={cn("border-t border-sidebar-border p-3", collapsed && "flex justify-center")}>
        <div className={cn("flex items-center gap-3", collapsed && "flex-col gap-0")}>
          <Avatar className="size-8">
            <AvatarFallback className="bg-sidebar-accent text-sidebar-foreground text-xs">LV</AvatarFallback>
          </Avatar>
          {!collapsed && (
            <div className="flex flex-col overflow-hidden">
              <span className="truncate text-sm font-medium">Dr. Leigh Vinocur</span>
              <span className="truncate text-xs text-sidebar-muted">Speaker</span>
            </div>
          )}
        </div>
      </div>
    </aside>
  )
}
