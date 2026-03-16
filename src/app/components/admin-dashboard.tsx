"use client"

import { useEffect, useState } from "react"
import {
  fetchAdminOverview,
  fetchAdminSpeakers,
  fetchAdminSpeakerLeads,
  adminLogin,
} from "@/lib/api"
import type { AdminOverview, AdminSpeaker, Lead } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Users,
  BarChart3,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  Search,
  ShieldCheck,
  Activity,
  Database,
  Bot,
} from "lucide-react"
import { cn } from "@/lib/utils"

// ── Constants ─────────────────────────────────────────────────────────────────
const TRIAGE_COLORS: Record<string, string> = {
  RED:    "#dc2626",
  YELLOW: "#d97706",
  GREEN:  "#16a34a",
}

const STATUS_STYLES: Record<string, string> = {
  New:       "bg-blue-100 text-blue-700",
  Contacted: "bg-purple-100 text-purple-700",
  Replied:   "bg-amber-100 text-amber-700",
  Booked:    "bg-green-100 text-green-700",
  Passed:    "bg-gray-100 text-gray-600",
  Rejected:  "bg-red-100 text-red-600",
}

const PLAN_STYLES: Record<string, string> = {
  Pro:     "bg-blue-100 text-blue-700",
  Starter: "bg-amber-100 text-amber-700",
  Free:    "bg-slate-100 text-slate-600",
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function TriageDot({ triage }: { triage: string }) {
  return (
    <span
      className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
      style={{ background: TRIAGE_COLORS[triage] ?? "#94a3b8" }}
    />
  )
}

function StatCard({
  icon: Icon,
  title,
  value,
  sub,
  accent,
}: {
  icon: React.ElementType
  title: string
  value: string | number
  sub: string
  accent?: string
}) {
  return (
    <Card>
      <CardContent className="pt-5 pb-4 px-5">
        <div className="flex items-start justify-between mb-3">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            {title}
          </p>
          <Icon className="size-4 text-muted-foreground/50" />
        </div>
        <p
          className="text-3xl font-bold tracking-tight"
          style={{ color: accent ?? "inherit" }}
        >
          {value}
        </p>
        <p className="text-xs text-muted-foreground mt-1">{sub}</p>
      </CardContent>
    </Card>
  )
}

// ── Login gate ────────────────────────────────────────────────────────────────
function LoginGate({ onLogin }: { onLogin: (pw: string) => void }) {
  const [pw, setPw]           = useState("")
  const [error, setError]     = useState("")
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError("")
    setLoading(true)
    try {
      await adminLogin(pw)
      sessionStorage.setItem("admin_pw", pw)
      onLogin(pw)
    } catch {
      setError("Invalid password. Try again.")
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center py-16">
      <Card className="w-full max-w-sm shadow-lg">
        <CardHeader className="pb-2">
          <div className="flex items-center gap-2 mb-1">
            <ShieldCheck className="size-4 text-blue-500" />
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-widest">
              Admin Access
            </span>
          </div>
          <CardTitle className="text-lg">Sign in to continue</CardTitle>
          <p className="text-sm text-muted-foreground">
            Enter your admin password to view the dashboard.
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-3">
            <Input
              type="password"
              placeholder="Admin password"
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              autoFocus
            />
            {error && <p className="text-destructive text-xs">{error}</p>}
            <Button type="submit" disabled={loading || !pw} className="w-full">
              {loading ? "Verifying…" : "Sign In"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

// ── Speaker row with expandable leads ─────────────────────────────────────────
function SpeakerRow({
  speaker,
  adminPw,
}: {
  speaker: AdminSpeaker
  adminPw: string
}) {
  const [expanded, setExpanded] = useState(false)
  const [leads, setLeads]       = useState<Lead[]>([])
  const [loading, setLoading]   = useState(false)
  const [loaded, setLoaded]     = useState(false)

  async function toggleExpand() {
    if (!expanded && !loaded) {
      setLoading(true)
      try {
        const data = await fetchAdminSpeakerLeads(speaker.speaker_id, adminPw)
        setLeads(data)
        setLoaded(true)
      } catch {
        setLeads([])
      } finally {
        setLoading(false)
      }
    }
    setExpanded((p) => !p)
  }

  return (
    <>
      <TableRow
        className="cursor-pointer hover:bg-muted/50 transition-colors"
        onClick={toggleExpand}
      >
        <TableCell className="pl-5">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-full bg-muted flex items-center justify-center text-xs font-semibold text-muted-foreground flex-shrink-0">
              {speaker.full_name?.[0]?.toUpperCase() ?? "?"}
            </div>
            <div className="min-w-0">
              <p className="font-medium text-sm truncate">{speaker.full_name}</p>
              <p className="text-xs text-muted-foreground truncate">{speaker.email}</p>
            </div>
          </div>
        </TableCell>
        <TableCell>
          <span className={cn(
            "text-xs font-semibold px-2 py-0.5 rounded-full",
            PLAN_STYLES[speaker.plan] ?? "bg-muted text-muted-foreground"
          )}>
            {speaker.plan || "Free"}
          </span>
        </TableCell>
        <TableCell className="text-xs text-muted-foreground font-mono">
          {speaker.speaker_id}
        </TableCell>
        <TableCell className="text-sm text-muted-foreground">
          {speaker.created_at
            ? new Date(speaker.created_at).toLocaleDateString()
            : "—"}
        </TableCell>
        <TableCell>
          <span className={cn(
            "text-xs px-2 py-0.5 rounded-full",
            speaker.status === "active"
              ? "bg-green-100 text-green-700"
              : "bg-muted text-muted-foreground"
          )}>
            {speaker.status || "active"}
          </span>
        </TableCell>
        <TableCell className="text-right pr-5">
          {expanded
            ? <ChevronUp className="size-4 text-muted-foreground ml-auto" />
            : <ChevronDown className="size-4 text-muted-foreground ml-auto" />}
        </TableCell>
      </TableRow>

      {expanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={6} className="p-0 bg-muted/30">
            <div className="border-t px-6 py-4">
              {loading ? (
                <div className="space-y-2">
                  {[1, 2, 3].map((i) => <Skeleton key={i} className="h-8 w-full" />)}
                </div>
              ) : leads.length === 0 ? (
                <p className="text-sm text-muted-foreground py-2">
                  No leads found for this speaker.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-muted-foreground border-b">
                        <th className="text-left pb-2 pr-4 font-medium">Triage</th>
                        <th className="text-left pb-2 pr-4 font-medium">Conference</th>
                        <th className="text-left pb-2 pr-4 font-medium">Score</th>
                        <th className="text-left pb-2 pr-4 font-medium">Status</th>
                        <th className="text-left pb-2 font-medium">Date Found</th>
                      </tr>
                    </thead>
                    <tbody>
                      {leads.map((lead) => (
                        <tr key={lead.id} className="border-b last:border-0">
                          <td className="py-2 pr-4">
                            <TriageDot triage={lead["Lead Triage"]} />
                          </td>
                          <td className="py-2 pr-4 font-medium max-w-[220px] truncate">
                            {lead["Conference Name"]}
                          </td>
                          <td className="py-2 pr-4 text-muted-foreground">
                            {lead["Match Score"]}/100
                          </td>
                          <td className="py-2 pr-4">
                            <span className={cn(
                              "px-1.5 py-0.5 rounded text-xs font-medium",
                              STATUS_STYLES[lead["Lead Status"]] ?? "bg-muted text-muted-foreground"
                            )}>
                              {lead["Lead Status"]}
                            </span>
                          </td>
                          <td className="py-2 text-muted-foreground">
                            {lead["Date Found"]
                              ? new Date(lead["Date Found"]).toLocaleDateString()
                              : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export function AdminDashboard() {
  const [authed, setAuthed]             = useState(false)
  const [adminPw, setAdminPw]           = useState("")
  const [overview, setOverview]         = useState<AdminOverview | null>(null)
  const [speakers, setSpeakers]         = useState<AdminSpeaker[]>([])
  const [loading, setLoading]           = useState(false)
  const [error, setError]               = useState("")
  const [search, setSearch]             = useState("")
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [lastRefresh, setLastRefresh]   = useState<Date | null>(null)

  useEffect(() => {
    const stored = sessionStorage.getItem("admin_pw")
    if (stored) {
      setAdminPw(stored)
      setAuthed(true)
    }
  }, [])

  useEffect(() => {
    if (authed && adminPw) loadData()
  }, [authed, adminPw])

  async function loadData() {
    setLoading(true)
    setError("")
    try {
      const [ov, sp] = await Promise.all([
        fetchAdminOverview(adminPw),
        fetchAdminSpeakers(),
      ])
      setOverview(ov)
      setSpeakers(sp.speakers)
      setLastRefresh(new Date())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load admin data")
    } finally {
      setLoading(false)
      setIsRefreshing(false)
    }
  }

  function handleLogin(pw: string) {
    setAdminPw(pw)
    setAuthed(true)
  }

  function handleLogout() {
    sessionStorage.removeItem("admin_pw")
    setAuthed(false)
    setAdminPw("")
    setOverview(null)
    setSpeakers([])
  }

  function handleRefresh() {
    setIsRefreshing(true)
    loadData()
  }

  if (!authed) return <LoginGate onLogin={handleLogin} />

  const filtered = speakers.filter((s) =>
    !search ||
    s.full_name?.toLowerCase().includes(search.toLowerCase()) ||
    s.email?.toLowerCase().includes(search.toLowerCase()) ||
    s.speaker_id?.toLowerCase().includes(search.toLowerCase())
  )

  const triage      = overview?.triage_breakdown ?? { RED: 0, YELLOW: 0, GREEN: 0 }
  const triageTotal = triage.RED + triage.YELLOW + triage.GREEN

  return (
    <div className="space-y-6">

      {/* Header row */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Admin Overview</h2>
          {lastRefresh && (
            <p className="text-xs text-muted-foreground mt-0.5">
              Updated {lastRefresh.toLocaleTimeString()}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={isRefreshing || loading}
          >
            <RefreshCw className={cn("size-4 mr-2", isRefreshing && "animate-spin")} />
            Refresh
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={handleLogout}
            className="text-muted-foreground text-xs"
          >
            Sign out
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-destructive/10 border border-destructive/20 text-destructive rounded-lg px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {/* Stat cards */}
      {loading && !overview ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {[1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-28 rounded-xl" />)}
        </div>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard icon={Users}       title="Total Speakers" value={overview?.total_speakers ?? 0} sub="registered accounts" />
          <StatCard icon={Database}    title="Total Leads"    value={overview?.total_leads ?? 0}    sub="across all speakers" />
          <StatCard icon={BarChart3}   title="Avg Score"      value={overview ? `${overview.avg_score}/100` : "—"} sub="lead quality" accent="#2563eb" />
          <StatCard icon={CalendarClock} title="Leads Today"  value={overview?.leads_today ?? 0}   sub="last 24 hours" accent="#16a34a" />
        </div>
      )}

      {/* Triage breakdown */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
            Lead Triage Breakdown
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {loading && !overview ? (
            <Skeleton className="h-8 w-full" />
          ) : (
            <>
              <div className="flex gap-6">
                {(["RED", "YELLOW", "GREEN"] as const).map((t) => (
                  <div key={t} className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full" style={{ background: TRIAGE_COLORS[t] }} />
                    <span className="text-sm text-muted-foreground">
                      {t === "RED" ? "Hot" : t === "YELLOW" ? "Warm" : "Cool"}
                    </span>
                    <span className="font-bold text-sm">{triage[t] ?? 0}</span>
                  </div>
                ))}
              </div>
              {triageTotal > 0 && (
                <div className="w-full flex h-2 rounded-full overflow-hidden bg-muted">
                  {(["RED", "YELLOW", "GREEN"] as const).map((t) => (
                    <div
                      key={t}
                      style={{
                        width: `${(triage[t] / triageTotal) * 100}%`,
                        background: TRIAGE_COLORS[t],
                      }}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* System health */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-2">
            <Activity className="size-4" />
            System Health
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-6 text-sm">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              <span className="text-muted-foreground">API</span>
              <span className="font-medium text-green-600">Operational</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-green-500" />
              <span className="text-muted-foreground">Airtable</span>
              <span className="font-medium text-green-600">Connected</span>
            </div>
            <div className="flex items-center gap-2">
              <Bot className="size-3.5 text-blue-500" />
              <span className="text-muted-foreground">Scout Agent</span>
              <span className="font-medium text-blue-600">Scheduled</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-muted-foreground/40" />
              <span className="text-muted-foreground">Backend</span>
              <span className="font-mono text-xs text-muted-foreground">
                {process.env.NEXT_PUBLIC_API_URL || "localhost:8000"}
              </span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Users table */}
      <Card>
        <CardHeader className="pb-3 flex flex-row items-center justify-between gap-4">
          <CardTitle className="text-sm font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-2">
            <Users className="size-4" />
            Users &amp; Request History
          </CardTitle>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
            <Input
              placeholder="Search users…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8 w-44 h-8 text-xs"
            />
          </div>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {loading && speakers.length === 0 ? (
            <div className="px-5 pb-5 space-y-3">
              {[1, 2, 3].map((i) => <Skeleton key={i} className="h-12 w-full" />)}
            </div>
          ) : filtered.length === 0 ? (
            <p className="text-sm text-muted-foreground px-5 pb-5">
              {search ? "No users match your search." : "No users found."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-5 text-xs">User</TableHead>
                  <TableHead className="text-xs">Plan</TableHead>
                  <TableHead className="text-xs">Speaker ID</TableHead>
                  <TableHead className="text-xs">Joined</TableHead>
                  <TableHead className="text-xs">Status</TableHead>
                  <TableHead className="text-xs text-right pr-5">Leads</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((s) => (
                  <SpeakerRow key={s.id} speaker={s} adminPw={adminPw} />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

    </div>
  )
}
