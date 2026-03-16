"use client"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import type { LeadStats } from "@/lib/types"

interface StatCardsProps {
  stats: LeadStats | null
  isLoading: boolean
}

export function StatCards({ stats, isLoading }: StatCardsProps) {
  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[...Array(4)].map((_, i) => (
          <Card key={i}>
            <CardHeader className="pb-2">
              <Skeleton className="h-4 w-24" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-8 w-16" />
              <Skeleton className="mt-1 h-3 w-20" />
            </CardContent>
          </Card>
        ))}
      </div>
    )
  }

  if (!stats) return null

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {/* Total Leads */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">Total Leads</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-bold">{stats.total}</div>
          <p className="text-xs text-muted-foreground">all time</p>
        </CardContent>
      </Card>

      {/* Hot Leads */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Hot Leads <span className="ml-1">🔥</span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-bold text-[#dc2626]">{stats.by_triage.RED}</div>
          <p className="text-xs text-muted-foreground">{"score >= 65"}</p>
        </CardContent>
      </Card>

      {/* Warm Leads */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">Warm Leads</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-bold text-[#d97706]">{stats.by_triage.YELLOW}</div>
          <p className="text-xs text-muted-foreground">score 35-64</p>
        </CardContent>
      </Card>

      {/* Avg Score */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">Avg Score</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-bold">
            {stats.avg_score.toFixed(1)}
            <span className="text-lg text-muted-foreground">/100</span>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
