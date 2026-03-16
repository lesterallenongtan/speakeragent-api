"use client"

import { useState, useEffect, useCallback } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import {
  Search,
  Brain,
  FileText,
  CheckCircle2,
  Loader2,
  RefreshCw,
  MapPin,
  Calendar,
  ExternalLink,
} from "lucide-react"
import { fetchLeads, fetchStats } from "@/lib/api"
import type { Lead, TriageLevel } from "@/lib/types"

interface ProcessingStep {
  id: number
  label: string
  description: string
  icon: React.ComponentType<{ className?: string }>
}

const processingSteps: ProcessingStep[] = [
  {
    id: 1,
    label: "Scanning conferences",
    description: "Searching through thousands of upcoming events and CFPs",
    icon: Search,
  },
  {
    id: 2,
    label: "Matching your expertise",
    description: "Analyzing alignment with your speaking topics and background",
    icon: Brain,
  },
  {
    id: 3,
    label: "Generating pitches",
    description: "Creating personalized hooks and talk suggestions",
    icon: FileText,
  },
  {
    id: 4,
    label: "Finalizing results",
    description: "Ranking and organizing your new opportunities",
    icon: CheckCircle2,
  },
]

const triageColors: Record<TriageLevel, string> = {
  RED:    "bg-red-500",
  YELLOW: "bg-amber-500",
  GREEN:  "bg-green-500",
}

const triageLabels: Record<TriageLevel, string> = {
  RED:    "Hot",
  YELLOW: "Warm",
  GREEN:  "Cool",
}

interface LeadsProcessingProps {
  onComplete?: () => void
  onLeadClick?: (lead: Lead) => void
}

export function LeadsProcessing({ onComplete, onLeadClick }: LeadsProcessingProps) {
  const [isProcessing, setIsProcessing]   = useState(false)
  const [isComplete, setIsComplete]       = useState(false)
  const [currentStep, setCurrentStep]     = useState(0)
  const [progress, setProgress]           = useState(0)
  const [stepProgress, setStepProgress]   = useState(0)
  const [hasRunOnce, setHasRunOnce]       = useState(false)
  const [foundLeads, setFoundLeads]       = useState<Lead[]>([])
  const [totalLeads, setTotalLeads]       = useState(0)
  const [showResults, setShowResults]     = useState(false)

  const startProcessing = useCallback(() => {
    setIsProcessing(true)
    setIsComplete(false)
    setCurrentStep(0)
    setProgress(0)
    setStepProgress(0)
    setFoundLeads([])
    setShowResults(false)
  }, [])

  // Auto-start on first mount
  useEffect(() => {
    if (!hasRunOnce) {
      setHasRunOnce(true)
      startProcessing()
    }
  }, [hasRunOnce, startProcessing])

  useEffect(() => {
    if (!isProcessing) return

    const stepDuration    = 3000
    const progressInterval = 100

    const interval = setInterval(() => {
      setStepProgress((prev) => {
        const newProgress = prev + (progressInterval / stepDuration) * 100

        if (newProgress >= 100) {
          setCurrentStep((prevStep) => {
            const nextStep = prevStep + 1
            if (nextStep >= processingSteps.length) {
              clearInterval(interval)
              setIsProcessing(false)
              setIsComplete(true)

              // Fetch ALL leads from Conferences table sorted by score
              Promise.all([
                fetchLeads(),
                fetchStats(),
              ]).then(([leadsData, stats]) => {
                // Sort by Match Score descending, take top 5
                const sorted = [...leadsData.leads].sort(
                  (a, b) => b["Match Score"] - a["Match Score"]
                )
                setFoundLeads(sorted.slice(0, 5))
                setTotalLeads(stats.total)
                setShowResults(true)
                setTimeout(() => onComplete?.(), 0)
              }).catch((err) => {
                console.error("Failed to fetch leads:", err)
                setShowResults(true)
                setTimeout(() => onComplete?.(), 0)
              })

              return prevStep
            }
            return nextStep
          })
          return 0
        }
        return newProgress
      })

      setProgress((prev) => {
        const increment =
          (progressInterval / (stepDuration * processingSteps.length)) * 100
        return Math.min(prev + increment, 100)
      })
    }, progressInterval)

    return () => clearInterval(interval)
  }, [isProcessing, onComplete])

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-lg font-semibold">Lead Scanner</CardTitle>
        <Button
          variant="outline"
          size="sm"
          onClick={startProcessing}
          disabled={isProcessing}
        >
          <RefreshCw className={cn("size-4 mr-2", isProcessing && "animate-spin")} />
          {isProcessing ? "Scanning..." : "Scan Again"}
        </Button>
      </CardHeader>

      <CardContent>
        {/* ── Processing state ───────────────────────────────────────────── */}
        {isProcessing && (
          <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center gap-4 p-4 rounded-lg bg-primary/5 border border-primary/10">
              <div className="flex items-center justify-center size-12 rounded-full bg-primary/10 shrink-0">
                <Loader2 className="size-6 text-primary animate-spin" />
              </div>
              <div>
                <h3 className="font-medium text-foreground">
                  Your leads are being prepared
                </h3>
                <p className="text-sm text-muted-foreground">
                  Estimated time: 2–5 minutes
                </p>
              </div>
            </div>

            {/* Overall progress */}
            <div>
              <div className="flex items-center justify-between text-sm mb-2">
                <span className="text-muted-foreground">Overall progress</span>
                <span className="font-mono text-foreground">
                  {Math.round(progress)}%
                </span>
              </div>
              <Progress value={progress} className="h-2" />
            </div>

            {/* Steps */}
            <div className="space-y-3">
              {processingSteps.map((step, index) => {
                const isActive      = index === currentStep
                const isStepComplete = index < currentStep
                const isPending     = index > currentStep
                const StepIcon      = step.icon

                return (
                  <div
                    key={step.id}
                    className={cn(
                      "flex items-start gap-3 p-3 rounded-lg transition-all duration-300",
                      isActive       && "bg-primary/5 ring-1 ring-primary/20",
                      isStepComplete && "bg-muted/50",
                      isPending      && "opacity-50"
                    )}
                  >
                    <div
                      className={cn(
                        "flex items-center justify-center size-8 rounded-full shrink-0 transition-colors",
                        isActive       && "bg-primary text-primary-foreground",
                        isStepComplete && "bg-green-500 text-white",
                        isPending      && "bg-muted text-muted-foreground"
                      )}
                    >
                      {isStepComplete ? (
                        <CheckCircle2 className="size-4" />
                      ) : isActive ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : (
                        <StepIcon className="size-4" />
                      )}
                    </div>

                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "text-sm font-medium",
                            isActive       && "text-primary",
                            isStepComplete && "text-foreground",
                            isPending      && "text-muted-foreground"
                          )}
                        >
                          {step.label}
                        </span>
                        {isStepComplete && (
                          <span className="text-xs text-green-600 font-medium">
                            Done
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {step.description}
                      </p>
                      {isActive && (
                        <div className="mt-2">
                          <Progress value={stepProgress} className="h-1" />
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* ── Results state ──────────────────────────────────────────────── */}
        {showResults && (
          <div className="space-y-4">
            {/* Success header */}
            <div className="flex items-center gap-4 p-4 rounded-lg bg-green-50 border border-green-200">
              <div className="flex items-center justify-center size-12 rounded-full bg-green-100 shrink-0">
                <CheckCircle2 className="size-6 text-green-600" />
              </div>
              <div className="flex-1">
                <h3 className="font-medium text-green-900">
                  {totalLeads > 0
                    ? `${totalLeads} lead${totalLeads !== 1 ? "s" : ""} found in your pipeline!`
                    : "Scan complete!"}
                </h3>
                <p className="text-sm text-green-700">
                  {foundLeads.length > 0
                    ? `Showing top ${foundLeads.length} by match score. Click any lead for details.`
                    : "No leads found yet — try running the scout script."}
                </p>
              </div>
            </div>

            {/* Lead list */}
            {foundLeads.length > 0 && (
              <div className="space-y-2">
                {foundLeads.map((lead) => (
                  <div
                    key={lead.id}
                    className="flex items-start gap-3 p-3 rounded-lg border bg-card hover:bg-accent/50 cursor-pointer transition-colors"
                    onClick={() => onLeadClick?.(lead)}
                  >
                    {/* Triage dot */}
                    <div
                      className={cn(
                        "mt-1 size-2.5 rounded-full shrink-0",
                        triageColors[lead["Lead Triage"]]
                      )}
                    />

                    <div className="flex-1 min-w-0">
                      <div className="flex items-start justify-between gap-2">
                        <h4 className="font-medium text-sm text-foreground line-clamp-1">
                          {lead["Conference Name"]}
                        </h4>
                        <div className="flex items-center gap-2 shrink-0">
                          <Badge variant="secondary" className="font-mono text-xs">
                            {lead["Match Score"]}/100
                          </Badge>
                          <Badge
                            className={cn(
                              "text-xs text-white",
                              triageColors[lead["Lead Triage"]]
                            )}
                          >
                            {triageLabels[lead["Lead Triage"]]}
                          </Badge>
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-xs text-muted-foreground">
                        {lead["Event Location"] && (
                          <span className="flex items-center gap-1">
                            <MapPin className="size-3" />
                            {lead["Event Location"]}
                          </span>
                        )}
                        {lead["Event Date"] && (
                          <span className="flex items-center gap-1">
                            <Calendar className="size-3" />
                            {new Date(lead["Event Date"]).toLocaleDateString()}
                          </span>
                        )}
                        {lead["Conference URL"] && (
                          <a
                            href={lead["Conference URL"]}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="flex items-center gap-1 text-primary hover:underline"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <ExternalLink className="size-3" />
                            Website
                          </a>
                        )}
                      </div>

                      {lead["Suggested Talk"] && (
                        <p className="mt-1 text-xs text-muted-foreground font-medium">
                          💡 {lead["Suggested Talk"]}
                        </p>
                      )}

                      {lead["The Hook"] && (
                        <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                          {lead["The Hook"]}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── Initial state ──────────────────────────────────────────────── */}
        {!isProcessing && !isComplete && !hasRunOnce && (
          <div className="flex items-center justify-center py-8 text-muted-foreground">
            <p>Starting lead scanner...</p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
