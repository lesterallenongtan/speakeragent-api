"use client"

import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { MapPin, Presentation } from "lucide-react"
import type { Lead, LeadStatus, TriageLevel } from "@/lib/types"
import { cn } from "@/lib/utils"

interface LeadCardProps {
  lead: Lead
  onClick: () => void
}

const triageColors: Record<TriageLevel, string> = {
  RED: "bg-[#dc2626]",
  YELLOW: "bg-[#d97706]",
  GREEN: "bg-[#16a34a]",
}

const statusColors: Record<LeadStatus, string> = {
  New: "bg-[#3b82f6] text-white",
  Contacted: "bg-[#8b5cf6] text-white",
  Replied: "bg-[#d97706] text-white",
  Booked: "bg-[#16a34a] text-white",
  Passed: "bg-[#6b7280] text-white",
}

export function LeadCard({ lead, onClick }: LeadCardProps) {
  return (
    <Card
      className="cursor-pointer transition-all hover:shadow-md hover:border-primary/20"
      onClick={onClick}
    >
      <CardContent className="p-4">
        <div className="flex items-start gap-3">
          {/* Triage dot */}
          <div className={cn("mt-1.5 size-2.5 rounded-full shrink-0", triageColors[lead["Lead Triage"]])} />

          <div className="flex-1 min-w-0 space-y-2">
            {/* Header row */}
            <div className="flex items-start justify-between gap-2">
              <h3 className="font-semibold text-foreground line-clamp-1">{lead["Conference Name"]}</h3>
              <Badge variant="secondary" className="shrink-0 font-mono text-xs">
                {lead["Match Score"]}/100
              </Badge>
            </div>

            {/* Details */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
              <span className="flex items-center gap-1">
                <MapPin className="size-3" />
                {lead["Event Location"]}
              </span>
              <span className="flex items-center gap-1">
                <Presentation className="size-3" />
                <span className="truncate max-w-[200px]">{lead["Suggested Talk"]}</span>
              </span>
            </div>

            {/* Status */}
            <div>
              <Badge className={cn("text-xs font-medium", statusColors[lead["Lead Status"]])}>
                {lead["Lead Status"]}
              </Badge>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
