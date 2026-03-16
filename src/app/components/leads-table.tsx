"use client"

import { useState } from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ArrowUpDown } from "lucide-react"
import type { Lead, LeadStatus, TriageLevel } from "@/lib/types"
import { cn } from "@/lib/utils"
import { updateLeadStatus } from "@/lib/api"

interface LeadsTableProps {
  leads: Lead[]
  isLoading: boolean
  onLeadClick: (lead: Lead) => void
  onStatusUpdate: (id: string, status: LeadStatus) => void
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

const statuses: LeadStatus[] = ["New", "Contacted", "Replied", "Booked", "Passed"]

type SortField = "score" | "date"
type SortOrder = "asc" | "desc"
type TriageFilter = "all" | TriageLevel
type StatusFilter = "all" | LeadStatus

export function LeadsTable({ leads, isLoading, onLeadClick, onStatusUpdate }: LeadsTableProps) {
  const [triageFilter, setTriageFilter] = useState<TriageFilter>("all")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all")
  const [sortField, setSortField] = useState<SortField>("score")
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc")
  const [updatingId, setUpdatingId] = useState<string | null>(null)

  const handleStatusChange = async (e: React.MouseEvent, leadId: string, status: LeadStatus) => {
    e.stopPropagation()
    setUpdatingId(leadId)
    try {
      await updateLeadStatus(leadId, status)
      onStatusUpdate(leadId, status)
    } catch {
      // Handle error silently
    } finally {
      setUpdatingId(null)
    }
  }

  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortOrder(sortOrder === "asc" ? "desc" : "asc")
    } else {
      setSortField(field)
      setSortOrder("desc")
    }
  }

  // Filter and sort leads
  const filteredLeads = leads
    .filter((lead) => {
      if (triageFilter !== "all" && lead["Lead Triage"] !== triageFilter) return false
      if (statusFilter !== "all" && lead["Lead Status"] !== statusFilter) return false
      return true
    })
    .sort((a, b) => {
      if (sortField === "score") {
        const diff = a["Match Score"] - b["Match Score"]
        return sortOrder === "asc" ? diff : -diff
      } else {
        const dateA = a["Date Found"] ? new Date(a["Date Found"]).getTime() : 0
        const dateB = b["Date Found"] ? new Date(b["Date Found"]).getTime() : 0
        const diff = dateA - dateB
        return sortOrder === "asc" ? diff : -diff
      }
    })

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="flex gap-4">
          <Skeleton className="h-10 w-48" />
          <Skeleton className="h-10 w-32" />
          <Skeleton className="h-10 w-32" />
        </div>
        <div className="border rounded-lg">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="flex items-center gap-4 p-4 border-b last:border-0">
              <Skeleton className="size-3 rounded-full" />
              <Skeleton className="h-5 w-48" />
              <Skeleton className="h-5 w-16" />
              <Skeleton className="h-5 w-32" />
              <Skeleton className="h-5 w-24" />
              <Skeleton className="h-8 w-28" />
              <Skeleton className="h-5 w-20" />
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-4">
        {/* Triage filter */}
        <div className="flex items-center gap-1">
          <span className="text-sm text-muted-foreground mr-2">Triage:</span>
          {(["all", "RED", "YELLOW", "GREEN"] as const).map((filter) => (
            <Button
              key={filter}
              variant={triageFilter === filter ? "default" : "outline"}
              size="sm"
              onClick={() => setTriageFilter(filter)}
              className="h-8"
            >
              {filter === "all" ? "All" : filter === "RED" ? "Hot" : filter === "YELLOW" ? "Warm" : "Cold"}
            </Button>
          ))}
        </div>

        {/* Status filter */}
        <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as StatusFilter)}>
          <SelectTrigger className="w-36 h-8">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Statuses</SelectItem>
            {statuses.map((status) => (
              <SelectItem key={status} value={status}>
                {status}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Sort */}
        <div className="flex items-center gap-2 ml-auto">
          <span className="text-sm text-muted-foreground">Sort:</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => toggleSort("score")}
            className={cn("h-8", sortField === "score" && "bg-muted")}
          >
            Score
            <ArrowUpDown className="size-3 ml-1" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => toggleSort("date")}
            className={cn("h-8", sortField === "date" && "bg-muted")}
          >
            Date Found
            <ArrowUpDown className="size-3 ml-1" />
          </Button>
        </div>
      </div>

      {/* Table */}
      <div className="border rounded-lg">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12">Triage</TableHead>
              <TableHead>Conference Name</TableHead>
              <TableHead className="w-20">Score</TableHead>
              <TableHead>Topic</TableHead>
              <TableHead>Location</TableHead>
              <TableHead className="w-32">Status</TableHead>
              <TableHead className="w-28">Date Found</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredLeads.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                  No leads found matching your filters
                </TableCell>
              </TableRow>
            ) : (
              filteredLeads.map((lead) => (
                <TableRow
                  key={lead.id}
                  onClick={() => onLeadClick(lead)}
                  className="cursor-pointer"
                >
                  <TableCell>
                    <div className={cn("size-3 rounded-full", triageColors[lead["Lead Triage"]])} />
                  </TableCell>
                  <TableCell className="font-medium">
                    {lead["Conference Name"].length > 40
                      ? `${lead["Conference Name"].slice(0, 40)}...`
                      : lead["Conference Name"]}
                  </TableCell>
                  <TableCell className="font-mono text-sm">
                    {lead["Match Score"]}/100
                  </TableCell>
                  <TableCell className="max-w-[200px] truncate text-muted-foreground">
                    {lead["Suggested Talk"]}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {lead["Event Location"]}
                  </TableCell>
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    <Select
                      value={lead["Lead Status"]}
                      onValueChange={(v) => handleStatusChange({} as React.MouseEvent, lead.id, v as LeadStatus)}
                      disabled={updatingId === lead.id}
                    >
                      <SelectTrigger
                        className={cn(
                          "h-7 w-28 text-xs border-0",
                          statusColors[lead["Lead Status"]]
                        )}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {statuses.map((status) => (
                          <SelectItem key={status} value={status}>
                            {status}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">
                    {lead["Date Found"]
                      ? new Date(lead["Date Found"]).toLocaleDateString("en-US", {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })
                      : "—"}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
