export type TriageLevel = "RED" | "YELLOW" | "GREEN"

export type LeadStatus = "New" | "Contacted" | "Replied" | "Booked" | "Passed"

export interface Lead {
  id: string
  "Conference Name": string
  "Lead Triage": TriageLevel
  "Match Score": number
  "Event Location": string
  "Event Date"?: string
  "The Hook": string
  CTA?: string
  "Lead Status": LeadStatus
  "Conference URL": string
  "Contact Email"?: string
  "Suggested Talk": string
  "Date Found"?: string
  speaker_id: string
  "Pay Estimate"?: string
}

export interface LeadStats {
  total: number
  by_triage: {
    RED: number
    YELLOW: number
    GREEN: number
  }
  by_status: {
    New: number
    Contacted: number
    Replied?: number
    Booked?: number
    Passed?: number
  }
  avg_score: number
}

export interface Speaker {
  id: string
  full_name: string
}

export interface DashboardResponse {
  speaker: Speaker
  stats: {
    total: number
    by_triage: {
      RED: number
      YELLOW: number
      GREEN: number
    }
    avg_score: number
  }
  top_leads: Lead[]
}

export interface LeadsResponse {
  count: number
  leads: Lead[]
}

// ── Admin types ───────────────────────────────────────────────────────────────

export interface AdminOverview {
  total_speakers: number
  total_leads: number
  avg_score: number
  leads_today: number
  triage_breakdown: {
    RED: number
    YELLOW: number
    GREEN: number
  }
}

export interface AdminSpeaker {
  id: string
  speaker_id: string
  full_name: string
  email: string
  plan: string
  status: string
  created_at: string
}

export interface AdminSpeakersResponse {
  count: number
  speakers: AdminSpeaker[]
}
