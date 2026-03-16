"use client"

import { useState, useEffect } from "react"
import { AppSidebar } from "@/components/app-sidebar"
import { StatCards } from "@/components/stat-cards"
import { LeadsTable } from "@/components/leads-table"
import { LeadDetailSheet } from "@/components/lead-detail-sheet"
import { LeadsProcessing } from "@/components/leads-processing"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { fetchDashboard, fetchLeads, fetchStats, mockLeads } from "@/lib/api"
import type { Lead, LeadStats, LeadStatus, DashboardResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { AdminDashboard } from "@/components/admin-dashboard"

type View = "dashboard" | "leads" | "settings" | "admin"

const triageColors = {
  RED: "bg-[#dc2626]",
  YELLOW: "bg-[#d97706]",
  GREEN: "bg-[#16a34a]",
}

export default function DashboardPage() {
  const [currentView, setCurrentView] = useState<View>("dashboard")
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null)
  const [stats, setStats] = useState<LeadStats | null>(null)
  const [leads, setLeads] = useState<Lead[]>([])
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null)
  const [isSheetOpen, setIsSheetOpen] = useState(false)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)

  const loadData = async () => {
    try {
      const [dashboardData, statsData, leadsData] = await Promise.all([
        fetchDashboard(),
        fetchStats(),
        fetchLeads(),
      ])
      setDashboard(dashboardData)
      setStats(statsData)
      setLeads(leadsData.leads)
    } catch (error) {
      console.error("Failed to load data:", error)
    } finally {
      setIsLoading(false)
      setIsRefreshing(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  const handleRefresh = () => {
    setIsRefreshing(true)
    loadData()
  }

  const handleLeadClick = (lead: Lead) => {
    setSelectedLead(lead)
    setIsSheetOpen(true)
  }

  const handleStatusUpdate = (id: string, status: LeadStatus) => {
    setLeads((prev) =>
      prev.map((lead) => (lead.id === id ? { ...lead, "Lead Status": status } : lead))
    )
    if (selectedLead?.id === id) {
      setSelectedLead({ ...selectedLead, "Lead Status": status })
    }
  }

  const handleCloseSheet = () => {
    setIsSheetOpen(false)
    setTimeout(() => setSelectedLead(null), 300)
  }

  return (
    <div className="flex min-h-screen bg-background">
      <AppSidebar
        currentView={currentView}
        onViewChange={setCurrentView}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
      />

      <main className="flex-1 overflow-auto">
        <div className="p-6 lg:p-8">
          {/* Header */}
          <div className="flex items-center justify-between mb-6">
            <div>
              <h1 className="text-2xl font-semibold text-foreground">
                {currentView === "dashboard"
                  ? "Dashboard"
                  : currentView === "leads"
                  ? "All Leads"
                  : currentView === "settings"
                  ? "Settings"
                  : "Admin"}
              </h1>
              <p className="text-muted-foreground text-sm">
                {currentView === "dashboard"
                  ? "Overview of your speaking engagement opportunities"
                  : currentView === "leads"
                  ? "Browse and manage all your leads"
                  : currentView === "settings"
                  ? "Manage your preferences"
                  : ""}
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              disabled={isRefreshing}
            >
              <RefreshCw
                className={cn("size-4 mr-2", isRefreshing && "animate-spin")}
              />
              Refresh
            </Button>
          </div>

          {/* Dashboard View */}
          {currentView === "dashboard" && (
            <div className="space-y-6">
              {/* Lead Scanner */}
              <LeadsProcessing onComplete={handleRefresh} onLeadClick={handleLeadClick} />

              {/* Stats */}
              <StatCards stats={stats} isLoading={isLoading} />

              {/* Top Leads */}
              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                  <CardTitle className="text-lg font-semibold">Hot Leads</CardTitle>
                  <Button variant="link" onClick={() => setCurrentView("leads")} className="text-sm">
                    View all
                  </Button>
                </CardHeader>
                <CardContent>
                  {isLoading ? (
                    <div className="space-y-3">
                      {[...Array(3)].map((_, i) => (
                        <div key={i} className="flex items-center gap-4 p-3 border rounded-lg">
                          <Skeleton className="size-3 rounded-full" />
                          <Skeleton className="h-5 flex-1" />
                          <Skeleton className="h-5 w-16" />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {(dashboard?.top_leads || leads.filter((l) => l["Lead Triage"] === "RED"))
                        .slice(0, 5)
                        .map((lead) => (
                          <button
                            key={lead.id}
                            onClick={() => handleLeadClick(lead)}
                            className="flex w-full items-center gap-4 p-3 border rounded-lg hover:bg-muted/50 transition-colors text-left"
                          >
                            <div
                              className={cn(
                                "size-3 rounded-full shrink-0",
                                triageColors[lead["Lead Triage"]]
                              )}
                            />
                            <div className="flex-1 min-w-0">
                              <p className="font-medium truncate">
                                {lead["Conference Name"]}
                              </p>
                              <p className="text-sm text-muted-foreground truncate">
                                {lead["Suggested Talk"]}
                              </p>
                            </div>
                            <Badge variant="secondary" className="shrink-0 font-mono text-xs">
                              {lead["Match Score"]}/100
                            </Badge>
                          </button>
                        ))}
                      {leads.filter((l) => l["Lead Triage"] === "RED").length === 0 && (
                        <p className="text-center text-muted-foreground py-8">
                          No hot leads found yet
                        </p>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>

              {/* Recent Activity */}
              <Card>
                <CardHeader>
                  <CardTitle className="text-lg font-semibold">Recent Activity</CardTitle>
                </CardHeader>
                <CardContent>
                  {isLoading ? (
                    <div className="space-y-4">
                      {[...Array(3)].map((_, i) => (
                        <div key={i} className="flex gap-3">
                          <Skeleton className="size-8 rounded-full" />
                          <div className="flex-1 space-y-1">
                            <Skeleton className="h-4 w-3/4" />
                            <Skeleton className="h-3 w-1/2" />
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="space-y-4">
                      {leads
                        .filter((l) => l["Date Found"])
                        .sort(
                          (a, b) =>
                            new Date(b["Date Found"]!).getTime() -
                            new Date(a["Date Found"]!).getTime()
                        )
                        .slice(0, 5)
                        .map((lead) => (
                          <div key={lead.id} className="flex gap-3">
                            <div
                              className={cn(
                                "mt-1 size-2 rounded-full shrink-0",
                                triageColors[lead["Lead Triage"]]
                              )}
                            />
                            <div className="flex-1 min-w-0">
                              <p className="text-sm">
                                <span className="font-medium">{lead["Conference Name"]}</span> was
                                found
                              </p>
                              <p className="text-xs text-muted-foreground">
                                {lead["Date Found"]
                                  ? new Date(lead["Date Found"]).toLocaleDateString("en-US", {
                                      month: "short",
                                      day: "numeric",
                                    })
                                  : "Recently"}
                              </p>
                            </div>
                          </div>
                        ))}
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>
          )}

          {/* Leads View */}
          {currentView === "leads" && (
            <LeadsTable
              leads={leads}
              isLoading={isLoading}
              onLeadClick={handleLeadClick}
              onStatusUpdate={handleStatusUpdate}
            />
          )}

          {/* Settings View */}
          {currentView === "settings" && (
            <Card>
              <CardHeader>
                <CardTitle>Settings</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-muted-foreground">
                  Settings panel coming soon. Here you&apos;ll be able to configure your speake
                  profile, notification preferences, and API integrations.
                </p>
              </CardContent>
            </Card>
          )}

          {currentView === "admin" && <AdminDashboard />}
        </div>
      </main>

      {/* Lead Detail Sheet */}
      <LeadDetailSheet
        lead={selectedLead}
        isLoading={false}
        isOpen={isSheetOpen}
        onClose={handleCloseSheet}
        onStatusChange={handleStatusUpdate}
      />
    </div>
  )
}
