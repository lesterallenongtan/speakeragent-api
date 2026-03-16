import type {
  AdminOverview,
  AdminSpeakersResponse,
  DashboardResponse,
  Lead,
  LeadsResponse,
  LeadStats,
  LeadStatus,
} from "./types"

const API_URL    = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
const API_KEY    = process.env.NEXT_PUBLIC_API_KEY || ""
const SPEAKER_ID = "leigh_vinocur"

// ── Auth headers ──────────────────────────────────────────────────────────────
function headers(adminPw?: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...(API_KEY  ? { "x-api-key": API_KEY }                : {}),
    ...(adminPw  ? { Authorization: `Bearer ${adminPw}` }  : {}),
  }
}

// ── Error helper ──────────────────────────────────────────────────────────────
async function handleResponse<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `${label} failed (${res.status})`)
  }
  return res.json()
}

// ── Speaker / leads API ───────────────────────────────────────────────────────

export async function fetchDashboard(): Promise<DashboardResponse> {
  const res = await fetch(`${API_URL}/api/dashboard/${SPEAKER_ID}`, {
    headers: headers(),
  })
  return handleResponse<DashboardResponse>(res, "fetchDashboard")
}

export async function fetchStats(): Promise<LeadStats> {
  const res = await fetch(
    `${API_URL}/api/leads/stats?speaker_id=${SPEAKER_ID}`,
    { headers: headers() }
  )
  return handleResponse<LeadStats>(res, "fetchStats")
}

export async function fetchLeads(
  triage?: string,
  status?: string
): Promise<LeadsResponse> {
  const params = new URLSearchParams({ speaker_id: SPEAKER_ID })
  if (triage && triage !== "All") params.set("triage", triage)
  if (status && status !== "All") params.set("status", status)
  const res = await fetch(`${API_URL}/api/leads?${params}`, {
    headers: headers(),
  })
  return handleResponse<LeadsResponse>(res, "fetchLeads")
}

export async function fetchLead(id: string): Promise<Lead> {
  const res = await fetch(`${API_URL}/api/leads/${id}`, {
    headers: headers(),
  })
  return handleResponse<Lead>(res, "fetchLead")
}

export async function updateLeadStatus(
  id: string,
  status: LeadStatus
): Promise<void> {
  const res = await fetch(`${API_URL}/api/leads/${id}/status`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ status }),
  })
  await handleResponse<unknown>(res, "updateLeadStatus")
}

// ── Admin API ─────────────────────────────────────────────────────────────────

/** Verify admin password. Throws if wrong. */
export async function adminLogin(password: string): Promise<void> {
  const res = await fetch(`${API_URL}/api/admin/login`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "x-api-key": API_KEY } : {}),
    },
    body: JSON.stringify({ password }),
  })
  await handleResponse<unknown>(res, "adminLogin")
}

/** High-level business metrics — requires admin password. */
export async function fetchAdminOverview(
  adminPw: string
): Promise<AdminOverview> {
  const res = await fetch(`${API_URL}/api/admin/overview`, {
    headers: headers(adminPw),
  })
  return handleResponse<AdminOverview>(res, "fetchAdminOverview")
}

/** List all active speakers. */
export async function fetchAdminSpeakers(): Promise<AdminSpeakersResponse> {
  const res = await fetch(`${API_URL}/api/admin/speakers`, {
    headers: headers(),
  })
  return handleResponse<AdminSpeakersResponse>(res, "fetchAdminSpeakers")
}

/** Get all leads for a specific speaker — requires admin password. */
export async function fetchAdminSpeakerLeads(
  speakerId: string,
  adminPw: string
): Promise<Lead[]> {
  const res = await fetch(
    `${API_URL}/api/admin/speakers/${speakerId}/leads`,
    { headers: headers(adminPw) }
  )
  const data = await handleResponse<{
    speaker_id: string
    count: number
    leads: Lead[]
  }>(res, "fetchAdminSpeakerLeads")
  return data.leads
}
