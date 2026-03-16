"use client"

import { useState } from "react"
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ExternalLink, Copy, CheckCircle, Calendar, MapPin, Presentation, DollarSign, Mail, Globe } from "lucide-react"
import type { Lead, LeadStatus, TriageLevel } from "@/lib/types"
import { cn } from "@/lib/utils"
import { updateLeadStatus } from "@/lib/api"

interface LeadDetailSheetProps {
  lead: Lead | null
  isLoading: boolean
  isOpen: boolean
  onClose: () => void
  onStatusChange: (id: string, status: LeadStatus) => void
}

const triageColors: Record<TriageLevel, { bg: string; text: string; label: string }> = {
  RED: { bg: "bg-[#dc2626]", text: "text-white", label: "Hot" },
  YELLOW: { bg: "bg-[#d97706]", text: "text-white", label: "Warm" },
  GREEN: { bg: "bg-[#16a34a]", text: "text-white", label: "Cold" },
}

const statuses: LeadStatus[] = ["New", "Contacted", "Replied", "Booked", "Passed"]

export function LeadDetailSheet({ lead, isLoading, isOpen, onClose, onStatusChange }: LeadDetailSheetProps) {
  const [copiedHook, setCopiedHook] = useState(false)
  const [isUpdating, setIsUpdating] = useState(false)

  const handleStatusChange = async (status: LeadStatus) => {
    if (!lead) return
    setIsUpdating(true)
    try {
      await updateLeadStatus(lead.id, status)
      onStatusChange(lead.id, status)
    } catch {
      // Handle error silently for now
    } finally {
      setIsUpdating(false)
    }
  }

  const handleCopyHook = async () => {
    if (!lead) return
    await navigator.clipboard.writeText(lead["The Hook"])
    setCopiedHook(true)
    setTimeout(() => setCopiedHook(false), 2000)
  }

  const handleMarkContacted = () => {
    if (!lead) return
    handleStatusChange("Contacted")
  }

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
        {isLoading ? (
          <div className="space-y-4 pt-6">
            <Skeleton className="h-8 w-3/4" />
            <Skeleton className="h-6 w-1/4" />
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : lead ? (
          <>
            <SheetHeader className="space-y-3 pb-4">
              <div className="flex items-start gap-3">
                <Badge className={cn("shrink-0", triageColors[lead["Lead Triage"]].bg, triageColors[lead["Lead Triage"]].text)}>
                  {triageColors[lead["Lead Triage"]].label}
                </Badge>
                <Badge variant="secondary" className="shrink-0 font-mono">
                  {lead["Match Score"]}/100
                </Badge>
              </div>
              <SheetTitle className="text-xl text-left">{lead["Conference Name"]}</SheetTitle>
            </SheetHeader>

            <div className="space-y-6">
              {/* Status selector */}
              <div className="space-y-2">
                <label className="text-sm font-medium text-muted-foreground">Status</label>
                <Select
                  value={lead["Lead Status"]}
                  onValueChange={(value) => handleStatusChange(value as LeadStatus)}
                  disabled={isUpdating}
                >
                  <SelectTrigger className="w-full">
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
              </div>

              {/* The Hook */}
              <div className="space-y-2">
                <label className="text-sm font-medium text-muted-foreground">The Hook</label>
                <blockquote className="border-l-4 border-primary/30 bg-muted/50 rounded-r-lg p-4 text-sm italic">
                  {lead["The Hook"]}
                </blockquote>
              </div>

              {/* CTA */}
              {lead.CTA && (
                <div className="space-y-2">
                  <label className="text-sm font-medium text-muted-foreground">Call to Action</label>
                  <p className="text-sm text-foreground bg-muted/30 rounded-lg p-3">
                    {lead.CTA}
                  </p>
                </div>
              )}

              {/* Details */}
              <div className="space-y-2">
                <label className="text-sm font-medium text-muted-foreground">Details</label>
                <div className="space-y-2 text-sm">
                  {lead["Event Date"] && (
                    <div className="flex items-center gap-2 text-muted-foreground">
                      <Calendar className="size-4" />
                      <span>
                        {new Date(lead["Event Date"]).toLocaleDateString("en-US", {
                          year: "numeric",
                          month: "long",
                          day: "numeric",
                        })}
                      </span>
                    </div>
                  )}
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <MapPin className="size-4" />
                    <span>{lead["Event Location"]}</span>
                  </div>
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Presentation className="size-4" />
                    <span>{lead["Suggested Talk"]}</span>
                  </div>
                  {lead["Pay Estimate"] && (
                    <div className="flex items-center gap-2 text-muted-foreground">
                      <DollarSign className="size-4" />
                      <span>{lead["Pay Estimate"]}</span>
                    </div>
                  )}
                </div>
              </div>

              {/* Contact */}
              <div className="space-y-2">
                <label className="text-sm font-medium text-muted-foreground">Contact</label>
                <div className="space-y-2 text-sm">
                  {lead["Contact Email"] && (
                    <a
                      href={`mailto:${lead["Contact Email"]}`}
                      className="flex items-center gap-2 text-primary hover:underline"
                    >
                      <Mail className="size-4" />
                      <span>{lead["Contact Email"]}</span>
                    </a>
                  )}
                  <a
                    href={lead["Conference URL"]}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-2 text-primary hover:underline"
                  >
                    <Globe className="size-4" />
                    <span className="truncate">{lead["Conference URL"]}</span>
                  </a>
                </div>
              </div>

              {/* Action buttons */}
              <div className="flex flex-col gap-2 pt-4 border-t">
                <Button asChild className="w-full">
                  <a href={lead["Conference URL"]} target="_blank" rel="noopener noreferrer">
                    <ExternalLink className="size-4 mr-2" />
                    Open Conference Website
                  </a>
                </Button>
                <div className="flex gap-2">
                  <Button variant="outline" onClick={handleCopyHook} className="flex-1">
                    {copiedHook ? (
                      <>
                        <CheckCircle className="size-4 mr-2" />
                        Copied!
                      </>
                    ) : (
                      <>
                        <Copy className="size-4 mr-2" />
                        Copy Hook
                      </>
                    )}
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={handleMarkContacted}
                    disabled={isUpdating || lead["Lead Status"] === "Contacted"}
                    className="flex-1"
                  >
                    Mark Contacted
                  </Button>
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-muted-foreground">
            No lead selected
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
