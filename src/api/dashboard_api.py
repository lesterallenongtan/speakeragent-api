"""FastAPI dashboard for SpeakerAgent.AI.

Serves endpoints that the frontend (Vercel/Next.js) calls to display leads.
Includes APScheduler for daily scout cron job.
"""

import json
import logging
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import List, Optional

import requests as http_requests
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import Settings
from src.api.airtable import AirtableAPI
from src.api.admin_api import router as admin_router
from src.api.checklist_api import router as checklist_router
from src.api.contact_api import router as contact_router
from src.api.persona_api import router as persona_router, _body_to_fields as _persona_fields_from_body
from src.api.deps import verify_api_key, TIER_MAX_PERSONAS

logger = logging.getLogger(__name__)


def _configure_logging():
    """Configure logging after uvicorn has finished setting up its own handlers."""
    level = getattr(logging, Settings.LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    # Ensure root has at least one handler
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(level)
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    for h in root.handlers:
        h.setFormatter(fmt)
    logging.getLogger('src').setLevel(level)
    logger.setLevel(level)

VALID_LEAD_STATUSES = {'New', 'Contacted', 'Replied', 'Booked', 'Passed', 'Rejected'}


# ── Scheduler ───────────────────────────────────────────────

def _ensure_profile_exists(speaker_id: str, profile_path: str) -> str:
    """Ensure a speaker profile JSON exists. Rebuild from Airtable if missing.

    Returns the (possibly updated) profile_path.
    """
    from src.api.profile_utils import build_profile_from_fields

    p = Path(profile_path)
    if p.exists():
        return profile_path

    logger.info(f"[SCOUT] Profile file missing: {profile_path}. Rebuilding from Airtable...")
    try:
        at = get_airtable()
        persona = at.get_persona(speaker_id)
        speaker = at.get_speaker(speaker_id)
        if not persona and not speaker:
            logger.warning(f"[SCOUT] Speaker {speaker_id} not found in Airtable either!")
            return profile_path

        fields = {**(speaker.get('fields', {}) if speaker else {}),
                  **(persona.get('fields', {}) if persona else {})}

        profile = build_profile_from_fields(fields)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w') as f:
            json.dump(profile, f, indent=2)
        logger.info(f"[SCOUT] Rebuilt profile for {speaker_id} → {profile_path}")

    except Exception as e:
        logger.error(f"[SCOUT] Failed to rebuild profile for {speaker_id}: {e}")

    return profile_path


# Max scout RUNS per week per tier
_TIER_MAX_SCOUTS = {
    'Free': 1,
    'Starter': 4,
    'Pro': 12,
}

# Max LEADS returned per scout run per tier (9999 = unlimited)
_TIER_MAX_LEADS = {
    'Free': 10,
    'Starter': 9999,
    'Pro': 9999,
}



def _check_and_reset_plan(speaker_id: str) -> Optional[tuple]:
    """Check the speaker's weekly scout quota, resetting if 7 days have passed.

    Returns (record, max_scout_runs, scouts_used, max_leads_per_run) if allowed,
    or None if the weekly run quota is exhausted.
    """
    from datetime import datetime
    try:
        at = get_airtable()
        record = at.get_speaker(speaker_id)
        if not record:
            logger.warning(f"[PLAN] Speaker {speaker_id} not found, skipping plan check")
            return None

        fields = record.get('fields', {})
        tier = (fields.get('Plan') or '').strip()
        max_scout_runs = _TIER_MAX_SCOUTS.get(tier, 1)
        max_leads_per_run = _TIER_MAX_LEADS.get(tier, Settings.MAX_LEADS_PER_RUN)
        scouts_used = int(fields.get('scouts_used') or 0)
        reset_date_str = fields.get('scouts_reset_date') or ''

        # Determine if weekly reset is due
        today = date.today()
        needs_reset = True
        if reset_date_str:
            try:
                reset_date = datetime.strptime(reset_date_str[:10], '%Y-%m-%d').date()
                needs_reset = (today - reset_date).days >= 7
            except ValueError:
                pass

        if needs_reset:
            scouts_used = 0
            at.update_speaker(record['id'], {
                'scouts_used': 0,
                'scouts_reset_date': today.isoformat(),
            })
            logger.info(f"[PLAN] Weekly reset for {speaker_id}: scouts_used → 0, reset_date → {today}")

        if scouts_used >= max_scout_runs:
            logger.info(f"[PLAN] {speaker_id} quota exhausted: {scouts_used}/{max_scout_runs} runs (tier={tier})")
            return None

        logger.info(f"[PLAN] {speaker_id} tier={tier} runs={scouts_used}/{max_scout_runs} leads_per_run={max_leads_per_run} — allowed")
        return record, max_scout_runs, scouts_used, max_leads_per_run

    except Exception as e:
        logger.warning(f"[PLAN] Plan check failed for {speaker_id}: {e}")
        return None


def _run_scout_for_speaker(speaker_id: str, profile_path: str, persona_record_id: str = ''):
    """Run scout pipeline for a single speaker/persona.

    persona_record_id: Airtable record ID of the Speaker_Persona row to update
    scout_status on. If omitted, falls back to the first persona for the speaker.
    """
    try:
        from src.agent.scout import run_scout
        # Ensure profile exists (rebuild from Airtable if container was redeployed)
        profile_path = _ensure_profile_exists(speaker_id, profile_path)

        # Check plan quota (weekly reset included)
        plan = _check_and_reset_plan(speaker_id)
        if plan is None:
            logger.info(f"[SCOUT] Skipping {speaker_id}: weekly quota exhausted or no plan")
            return {'skipped': 'quota_exhausted'}
        record, _, scouts_used, max_leads_per_run = plan

        at = get_airtable()
        # Resolve which persona row to stamp scout_status on
        if persona_record_id:
            persona = at.get_persona_by_id(persona_record_id)
        else:
            persona = at.get_persona(speaker_id)
        if persona:
            at.update_persona(persona['id'], {'scout_status': 'Running'})
        logger.info(f"[SCOUT] Starting scout for {speaker_id} with profile {profile_path}")
        try:
            summary = run_scout(
                profile_path=profile_path,
                speaker_id=speaker_id,
                max_leads=max_leads_per_run,
                persona_record_id=persona_record_id,
            )
        finally:
            if persona:
                logger.info(f"[SCOUT] Marking scout as Completed for {speaker_id} persona {persona_record_id}")
                at.update_persona(persona['id'], {'scout_status': 'Completed'})

        logger.info(
            f"[SCOUT] Complete for {speaker_id}: "
            f"urls={summary.get('total_urls', 0)} "
            f"scraped={summary.get('scraped', 0)} "
            f"scored={summary.get('scored', 0)} "
            f"pushed={summary.get('pushed', 0)} "
            f"dupes={summary.get('skipped_duplicate', 0)} "
            f"scrape_fail={summary.get('skipped_scrape_fail', 0)} "
            f"score_fail={summary.get('skipped_score_fail', 0)}"
        )

        # Increment scouts_used in Airtable
        new_count = scouts_used + 1
        update_result = at.update_speaker(record['id'], {'scouts_used': new_count})
        if update_result:
            logger.info(f"[SCOUT] scouts_used updated to {new_count} for {speaker_id}")
        else:
            logger.warning(f"[SCOUT] Failed to update scouts_used for {speaker_id} — check Airtable field name/type")

        return summary
    except Exception as e:
        logger.error(f"[SCOUT] Failed for {speaker_id}: {e}", exc_info=True)
        return {'error': str(e)}


def _run_daily_scout():
    """Run the scout pipeline for ALL active speakers."""
    try:
        settings = Settings()
        at = AirtableAPI(
            api_key=settings.AIRTABLE_API_KEY,
            base_id=settings.AIRTABLE_BASE_ID,
            leads_table=settings.LEADS_TABLE,
            speakers_table=settings.SPEAKERS_TABLE,
            persona_table=settings.PERSONA_TABLE,
        )
        active_speakers = at.list_active_personas()
        if not active_speakers:
            logger.warning("[CRON] No active speakers found. Falling back to default.")
            _run_scout_for_speaker(
                settings.SCOUT_SPEAKER_ID,
                settings.SCOUT_PROFILE_PATH,
            )
            return

        logger.info(f"[CRON] Running daily scout for {len(active_speakers)} active speaker(s)")
        for record in active_speakers:
            fields = record.get('fields', {})
            sid = fields.get('speaker_id', '')
            persona_record_id = record.get('id', '')
            if not sid:
                continue
            profile_path = f"config/speaker_profiles/{sid}_{persona_record_id}.json"
            _run_scout_for_speaker(sid, profile_path, persona_record_id)

    except Exception as e:
        logger.error(f"[CRON] Daily scout failed: {e}", exc_info=True)


_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler on startup, shut down on exit."""
    _configure_logging()
    global _scheduler
    settings = Settings()
    if settings.ENABLE_CRON:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        _scheduler = BackgroundScheduler()
        # 6 AM EST = 11:00 UTC
        _scheduler.add_job(
            _run_daily_scout,
            CronTrigger(hour=11, minute=0, timezone='UTC'),
            id='daily_scout',
            replace_existing=True,
        )
        _scheduler.start()
        logger.info("[CRON] Daily scout scheduled for 11:00 UTC (6 AM EST)")
    yield
    if _scheduler:
        _scheduler.shutdown()
        logger.info("[CRON] Scheduler shut down")


# ── App ─────────────────────────────────────────────────────

app = FastAPI(
    title="SpeakerAgent.AI Dashboard API",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(admin_router)
app.include_router(checklist_router)
app.include_router(contact_router)
app.include_router(persona_router)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"Validation error for {request.url}: {exc.errors()}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )

# CORS — driven by CORS_ORIGINS env var (comma-separated)
_allowed_origins = os.getenv('CORS_ORIGINS', 'http://localhost:3000').split(',')
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Init on startup
_settings = None
_airtable = None


def get_airtable() -> AirtableAPI:
    global _settings, _airtable
    if _airtable is None:
        _settings = Settings()
        _airtable = AirtableAPI(
            api_key=_settings.AIRTABLE_API_KEY,
            base_id=_settings.AIRTABLE_BASE_ID,
            leads_table=_settings.LEADS_TABLE,
            speakers_table=_settings.SPEAKERS_TABLE,
            persona_table=_settings.PERSONA_TABLE,
        )
    return _airtable


class StatusUpdate(BaseModel):
    status: str
    notes: Optional[str] = None
    updated_by: Optional[str] = None


class MessageUpdate(BaseModel):
    message: str
    updated_by: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    subject: Optional[str] = None


class SpeakerTopic(BaseModel):
    title: str
    abstract: Optional[str] = ""
    audience: Optional[str] = ""

class EmailAttachment(BaseModel):
    filename: str
    content: str  # base64-encoded file content
    type: Optional[str] = "application/octet-stream"


class SpeakerUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    tagline: Optional[str] = None
    bio: Optional[str] = None
    topics: Optional[List[SpeakerTopic]] = None
    target_industries: Optional[List[str]] = None
    min_honorarium: Optional[int] = None
    years_experience: Optional[int] = None
    location: Optional[str] = None
    website: Optional[str] = None
    credentials: Optional[str] = None
    linkedin: Optional[str] = None
    speaker_sheet: Optional[str] = None
    notes: Optional[str] = None
    conference_year: Optional[int] = None
    conference_tier: Optional[str] = None
    zip_code: Optional[str] = None
    attachments: Optional[List[EmailAttachment]] = None


class SpeakerRegistration(BaseModel):
    full_name: str
    email: str
    tagline: Optional[str] = None
    bio: Optional[str] = None
    topics: Optional[List[SpeakerTopic]] = None
    target_industries: Optional[List[str]] = None
    min_honorarium: Optional[int] = None
    years_experience: Optional[int] = None
    location: Optional[str] = None
    website: Optional[str] = None
    credentials: Optional[str] = None
    linkedin: Optional[str] = None
    speaker_sheet: Optional[str] = None
    notes: Optional[str] = None
    conference_year: Optional[int] = None
    conference_tier: Optional[str] = None
    zip_code: Optional[str] = None
    attachments: Optional[List[EmailAttachment]] = None
    # persona_name: Optional[str] = None


# ── Health ──────────────────────────────────────────────────

@app.get("/health")
def health_check():
    cron_active = _scheduler is not None and _scheduler.running if _scheduler else False
    return {"status": "ok", "cron_active": cron_active}


class SendEmailRequest(BaseModel):
    emailFrom: Optional[str] = None
    to: List[str]
    subject: str
    content: str
    content_type: Optional[str] = "text/html"
    attachments: Optional[List[EmailAttachment]] = None


@app.post("/api/send-email")
def send_email(body: SendEmailRequest, _: None = Depends(verify_api_key)):
    """Send an email via SendGrid with optional attachments."""
    sendgrid_key = os.getenv('SENDGRID_API_KEY', '')
    if not sendgrid_key:
        raise HTTPException(status_code=503, detail="SENDGRID_API_KEY not configured")

    email_from = os.getenv('EMAIL_FROM', 'tony@speakeragent.ai')
    # email_from = body.emailFrom if body.emailFrom else default_from
    # email_from =  default_from
    logger.info(f"Preparing to send email from {email_from} to {body.to}")
    payload = {
        'personalizations': [{'to': [{'email': addr} for addr in body.to]}],
        'from': {'email': email_from},
        'subject': body.subject,
        'content': [{'type': body.content_type, 'value': body.content}],
    }

    if body.attachments:
        payload['attachments'] = [
            {'filename': a.filename, 'content': a.content, 'type': a.type}
            for a in body.attachments
        ]

    try:
        resp = http_requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={
                'Authorization': f'Bearer {sendgrid_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=10,
        )
        if resp.status_code == 202:
            logger.info(f"[EMAIL] Sent '{body.subject}' to {body.to}")
            return {"status": "sent", "to": body.to}
        else:
            logger.error(f"[EMAIL] SendGrid error: {resp.status_code} {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[EMAIL] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Leads ───────────────────────────────────────────────────

@app.get("/api/leads")
def list_leads(
    speaker_id: str = Query(..., description="Speaker ID to filter by"),
    status: Optional[str] = Query(None),
    triage: Optional[str] = Query(None),
    _: None = Depends(verify_api_key),
):
    """Get all leads for a speaker, with optional filters."""
    at = get_airtable()
    records = at.get_leads(
        speaker_id=speaker_id,
        status=status or '',
        triage=triage or '',
    )
    return {
        "count": len(records),
        "leads": [
            {"id": r["id"], **r.get("fields", {})}
            for r in records
        ]
    }


@app.get("/api/leads/stats")
def lead_stats(speaker_id: str = Query(...), _: None = Depends(verify_api_key)):
    """Aggregated lead statistics for a speaker."""
    at = get_airtable()
    stats = at.get_lead_stats(speaker_id)
    return stats


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: str, _: None = Depends(verify_api_key)):
    """Get a single lead by Airtable record ID."""
    at = get_airtable()
    record = at.get_lead_by_id(lead_id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"id": record["id"], **record.get("fields", {})}


@app.put("/api/leads/{lead_id}/status")
def update_lead_status(lead_id: str, body: StatusUpdate, _: None = Depends(verify_api_key)):
    """Update lead status (New -> Contacted -> Replied -> Booked -> Passed -> Rejected)."""
    if body.status not in VALID_LEAD_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(VALID_LEAD_STATUSES))}"
        )
    at = get_airtable()



    # logger.info(f"Lead {lead_id} updating status to {body.status}")
    result = at.update_lead(lead_id, {'Lead Status': body.status, 'Update Notes': body.notes or '', 'Updated By': body.updated_by or '', 'Update Timestamp': date.today().isoformat()})
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update lead")

    if body.status == 'Contacted':
        threading.Thread(
            target=_send_outreach_email,
            args=(at, lead_id, result.get('fields', {})),
            daemon=True,
        ).start()

    return {"id": result["id"], **result.get("fields", {})}


@app.put("/api/leads/{lead_id}/message")
def update_lead_message(lead_id: str, body: MessageUpdate, _: None = Depends(verify_api_key)):
    """Update lead approval message."""
    at = get_airtable()
    logger.info(f"Lead {lead_id} updating approval message")
    result = at.update_lead(lead_id, {'Approval Message': body.message, 'Suggested Talk': body.subject or '', 'Updated By': body.updated_by or '', 'Update Timestamp': date.today().isoformat(), 'Contact Name': body.contact_name or '', 'Contact Email': body.contact_email or ''})
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update lead message")
    return {"id": result["id"], **result.get("fields", {})}

# ── Directory builder (multi-agent) ────────────────────────

def _run_build_directory(profile_path: str, output_path: str):
    """Background task: run the multi-agent directory builder."""
    try:
        from scripts.build_directory import build_directory
        result = build_directory(profile_path=profile_path, output_path=output_path)
        logger.info(f"[DIRECTORY] Built {len(result.get('urls', []))} URLs → {output_path}")
    except Exception as e:
        logger.error(f"[DIRECTORY] Build failed: {e}", exc_info=True)


@app.post("/api/directory/build")
def trigger_directory_build(
    speaker_id: Optional[str] = Query(None),
    _: None = Depends(verify_api_key),
):
    """Trigger the multi-agent directory builder for a speaker profile."""
    sid = speaker_id or Settings.SCOUT_SPEAKER_ID
    profile_path = f"config/speaker_profiles/{sid}.json"
    output_path = "config/seed_urls.json"
    if not Path(profile_path).exists():
        raise HTTPException(status_code=404, detail=f"Profile not found: {profile_path}")
    thread = threading.Thread(
        target=_run_build_directory,
        args=(profile_path, output_path),
        daemon=True,
    )
    thread.start()
    return {
        "status": "started",
        "speaker_id": sid,
        "profile_path": profile_path,
        "output_path": output_path,
        "message": "Multi-agent directory build running in background (researcher → devil's advocate → editor)",
    }


# ── Scout (manual trigger) ─────────────────────────────────

@app.post("/api/scout/run")
def trigger_scout(
    speaker_id: Optional[str] = Query(None),
    persona_id: Optional[str] = Query(None),
    _: None = Depends(verify_api_key),
):
    """Manually trigger a scout run.

    - No params: runs scout for all active speakers.
    - speaker_id only: runs for the speaker's default (first) persona.
    - speaker_id + persona_id: runs for that specific persona.
    """
    if speaker_id:
        plan_info = _check_and_reset_plan(speaker_id)
        if plan_info is None:
            raise HTTPException(status_code=429, detail="Scout quota exhausted for this billing period")
        _, max_scout_runs, scouts_used, _ = plan_info
        remaining = max_scout_runs - scouts_used
        if remaining <= 0:
            raise HTTPException(
                status_code=429,
                detail=f"Scout quota exhausted ({scouts_used}/{max_scout_runs} runs used). Resets weekly.",
            )

        # Resolve profile path: per-persona file if persona_id provided, else default
        if persona_id:
            at = get_airtable()
            persona = at.get_persona_by_id(persona_id)
            if not persona:
                raise HTTPException(status_code=404, detail="Persona not found")
            if persona.get('fields', {}).get('speaker_id') != speaker_id:
                raise HTTPException(status_code=403, detail="Persona does not belong to this speaker")
            profile_path = f"config/speaker_profiles/{speaker_id}_{persona_id}.json"
        else:
            profile_path = f"config/speaker_profiles/{speaker_id}.json"

        thread = threading.Thread(
            target=_run_scout_for_speaker,
            args=(speaker_id, profile_path, persona_id or ''),
            daemon=True,
        )
        thread.start()
        return {
            "status": "started",
            "speaker_id": speaker_id,
            "persona_id": persona_id,
            "scouts_remaining": remaining - 1,
        }
    else:
        thread = threading.Thread(target=_run_daily_scout, daemon=True)
        thread.start()
        return {"status": "started", "message": "Scout running for all active speakers"}


@app.get("/api/scout/status/{speaker_id}")
def get_scout_status(speaker_id: str, _: None = Depends(verify_api_key)):
    """Return the current scout_status for a speaker (from Speaker_Persona)."""
    at = get_airtable()
    persona = at.get_persona(speaker_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Speaker not found")
    status = persona.get("fields", {}).get("scout_status", None)
    return {"speaker_id": speaker_id, "scout_status": status}


@app.get("/api/speakers/by-email/{email}")
def get_personas_by_email(email: str, _: None = Depends(verify_api_key)):
    """Return all personas for a given email (joins Speakers + Speaker_Persona)."""
    at = get_airtable()
    speaker_records = at.list_speakers_by_email(email)
    if not speaker_records:
        return {"email": email, "personas": [], "count": 0}
    personas = []
    for r in speaker_records:
        sf = r["fields"]
        sid = sf.get("speaker_id", "")
        persona = at.get_persona(sid) if sid else None
        pf = persona.get("fields", {}) if persona else {}
        personas.append({
            "speaker_id": sid,
            "full_name": sf.get("full_name"),
            # "persona_name": pf.get("persona_name") or sf.get("full_name"),
            "plan": sf.get("Plan", "Free"),
            "scouts_used": sf.get("scouts_used", 0),
            "status": pf.get("status"),
            "scout_status": pf.get("scout_status"),
            "created_at": pf.get("created_at"),
            "topics": pf.get("topics"),
        })
    return {"email": email, "personas": personas, "count": len(personas)}


# ── Email ──────────────────────────────────────────────────

def _send_welcome_email(emailTo: str, full_name: str, speaker_id: str, attachments: Optional[List[EmailAttachment]] = None):
    """Send a welcome email to a newly registered speaker."""
    frontend_url = os.getenv('FRONTEND_URL', 'https://frontend-production-4a8a.up.railway.app')

    html_content = f'''
<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.7;color:#1a1a1a;max-width:600px;">
  <h2 style="color:#1e40af;margin-bottom:8px;">Welcome to SpeakerAgent.AI, {full_name}!</h2>
  <p style="margin:0 0 16px 0;">We're thrilled to have you on board. Your profile is all set up and our AI Scout is already being configured to find speaking engagements that match your expertise.</p>
  <p style="margin:0 0 24px 0;">You'll start seeing curated leads in your dashboard soon — conferences, podcasts, and corporate events tailored specifically to your topics and industry.</p>
  <p style="margin:0 0 24px 0;">
    <a href="{frontend_url}" style="background:#1e40af;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;">Go to Your Dashboard</a>
  </p>
  <p style="margin:0 0 4px 0;">Warm regards,</p>
  <p style="margin:0;font-weight:bold;">The SpeakerAgent.AI Team</p>
</div>'''

    try:
        send_email(SendEmailRequest(
            to=[emailTo],
            subject='Welcome to SpeakerAgent.AI!',
            content=html_content,
            content_type='text/html',
            attachments=attachments,
        ))
        logger.info(f"[EMAIL] Welcome email sent to {emailTo} for {speaker_id}")
    except Exception as e:
        logger.error(f"[EMAIL] Error sending welcome email for {speaker_id}: {e}")


def _send_outreach_email(emailFrom:str, at: AirtableAPI, lead_id: str, fields: dict):
    """Send outreach email to a conference contact when lead is marked Contacted."""
    contact_email = fields.get('Contact Email', '')
    if not contact_email:
        logger.info(f"[EMAIL] No contact email for lead {lead_id}, skipping outreach")
        return

    subject = fields.get('Suggested Talk', 'Speaking Opportunity')
    contact_name = fields.get('Contact Name', '')

    # Resolve speaker full name
    speaker_name = 'Speaker'
    speaker_id = fields.get('speaker_id', '')
    if speaker_id:
        try:
            speaker_record = at.get_speaker(speaker_id)
            if speaker_record:
                speaker_name = speaker_record.get('fields', {}).get('full_name', 'Speaker')
        except Exception as e:
            logger.warning(f"[EMAIL] Failed to get speaker {speaker_id}: {e}")

    # Build body
    approval_message = fields.get('Approval Message', '')
    if approval_message:
        # Wrap existing plain-text approval message in styled HTML
        paragraphs = ''.join(
            f'<p style="margin:0 0 16px 0;">{line}</p>' if line.strip() else '<br>'
            for line in approval_message.splitlines()
        )
        html_content = f'''<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#1a1a1a;">{paragraphs}</div>'''
    else:
        greeting = f"Dear {contact_name}," if contact_name else "Dear Event Organizer,"
        hook = fields.get('The Hook', '')
        cta = fields.get('CTA', '')
        html_content = f'''
<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#1a1a1a;max-width:600px;">
  <p style="margin:0 0 20px 0;">{greeting}</p>
  <p style="margin:0 0 20px 0;">{hook}</p>
  <p style="margin:0 0 28px 0;">{cta}</p>
  <p style="margin:0 0 4px 0;">Warm regards,</p>
  <p style="margin:0;font-weight:bold;">{speaker_name}</p>
</div>'''

    try:
        send_email(SendEmailRequest(
            emailFrom= emailFrom,
            to=[contact_email],
            subject=subject,
            content=html_content,
            content_type='text/html',
        ))
        logger.info(f"[EMAIL] Outreach sent for lead {lead_id} to {contact_email}")
    except Exception as e:
        logger.error(f"[EMAIL] Error sending outreach for lead {lead_id}: {e}")


def _clean_profile_with_ai(profile: dict) -> dict:
    """Use Claude to clean and normalize free-text profile fields before scout runs.

    Fixes stream-of-consciousness input, dictation artifacts, missing punctuation,
    run-on sentences, and inconsistent formatting without changing meaning.
    Returns the original profile unchanged if cleaning fails.
    """
    api_key = os.getenv('CLAUDE_API_KEY', '')
    if not api_key:
        logger.warning("[CLEAN] CLAUDE_API_KEY not set, skipping profile cleaning")
        return profile

    fields_to_clean = {
        'professional_title': profile.get('professional_title', ''),
        'credentials': profile.get('credentials', ''),
        'bio': profile.get('bio', ''),
        'topics': profile.get('topics', []),
        'target_industries': profile.get('target_industries', []),
        'discussion_points': profile.get('discussion_points', []),
    }

    prompt = f"""You are a data cleaning assistant for a speaker profile system.

Clean and normalize the following speaker profile fields. Fix only formatting issues:
- Stream-of-consciousness or dictation output → add proper punctuation and capitalization
- Run-on sentences → split or punctuate correctly
- Missing commas in lists → restore them
- Obvious typos and capitalization errors
- Inconsistent spacing

Rules:
- Do NOT change the meaning, add content, or remove information
- Keep lists as arrays, keep strings as strings
- Return ONLY a valid JSON object with exactly the same keys
- If a field is empty or cannot be cleaned, return it as an empty string or empty array, but do not remove keys
- Do NOT include any explanatory text, apologies, or disclaimers in the output
- Do NOT hallucinate information or make assumptions beyond basic formatting fixes

INPUT:
{json.dumps(fields_to_clean, indent=2)}

Return JSON only, no markdown, no extra text."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = os.getenv('CLAUDE_HAIKU_MODEL', 'claude-haiku-4-5-20251001')
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = response.content[0].text.strip()
        start = text.find('{')
        end = text.rfind('}')
        if start == -1 or end == -1:
            logger.warning("[CLEAN] No JSON in Claude response, using original profile")
            return profile
        cleaned = json.loads(text[start:end + 1])
        # Merge only non-empty cleaned values back
        for key, val in cleaned.items():
            if val:
                profile[key] = val
        logger.info(f"[CLEAN] Profile fields cleaned: {list(cleaned.keys())}")
        return profile
    except Exception as e:
        logger.warning(f"[CLEAN] Profile cleaning failed: {e}, using original")
        return profile


def _create_profile_and_run_scout(speaker_id: str, body, persona_record_id: str = ''):
    """Create a speaker profile JSON from registration data and trigger scout."""
    from src.api.profile_utils import create_profile_and_run_scout
    create_profile_and_run_scout(
        speaker_id,
        persona_record_id,
        body=body,
        profile_cleaner=_clean_profile_with_ai,
    )


# ── Speaker ─────────────────────────────────────────────────

@app.post("/api/speakers/register")
def register_speaker(body: SpeakerRegistration):
    """Register a new speaker. Generates a unique speaker_id."""
    at = get_airtable()

    # Enforce persona limit per email
    existing_personas = at.list_speakers_by_email(body.email)
    if existing_personas:
        plan = (existing_personas[0].get('fields', {}).get('Plan') or 'Free').strip()
        max_personas = TIER_MAX_PERSONAS.get(plan, 1)
        if len(existing_personas) >= max_personas:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": f"Persona limit reached for your {plan} plan ({max_personas} persona{'s' if max_personas > 1 else ''} allowed).",
                    "plan": plan,
                    "max_personas": max_personas,
                    "current_personas": len(existing_personas),
                }
            )

    # Generate unique speaker_id: slug from name + short UUID
    name_slug = body.full_name.lower().replace(' ', '_').replace('.', '')
    name_slug = ''.join(c for c in name_slug if c.isalnum() or c == '_')
    short_uuid = uuid.uuid4().hex[:8]
    speaker_id = f"{name_slug}_{short_uuid}"

    # Create Speakers row — identity + billing only
    speaker_fields = {
        'speaker_id': speaker_id,
        'full_name': body.full_name,
        'email': body.email,
        'Plan': 'Pro',
        'scouts_used': 0,
        'scouts_reset_date': date.today().isoformat(),
        'status': 'active',
        'created_at': date.today().isoformat(),
    }
    record = at.create_speaker(speaker_fields)
    if not record:
        raise HTTPException(status_code=500, detail="Failed to create speaker")

    # Create Speaker_Persona row — profile details
    persona_fields = _persona_fields_from_body(body)
    persona_fields['speaker_id'] = speaker_id
    persona_fields['created_at'] = date.today().isoformat()
    # persona_fields.setdefault('status', 'active')
    persona_record = at.create_persona(persona_fields)
    if not persona_record:
        # Roll back the Speakers row so data stays consistent
        at.delete_speaker(record['id'])
        raise HTTPException(status_code=500, detail="Failed to create Persona")

    # Create onboarding checklist (non-blocking)
    threading.Thread(
        target=lambda: get_airtable().create_onboarding_checklist(speaker_id),
        daemon=True,
    ).start()

    # Upload attachments to Speaker_Persona row (non-blocking)
    if body.attachments and persona_record:
        def _upload_attachments(record_id: str, attachments):
            field_name = os.getenv('AIRTABLE_ATTACHMENT_FIELD', 'Attachments')
            _at = get_airtable()
            for attachment in attachments:
                try:
                    _at.upload_attachment(record_id, field_name, attachment.filename, attachment.content, attachment.type or 'application/octet-stream')
                except Exception as e:
                    logger.error(f"Failed to upload attachment '{attachment.filename}': {e}")
        threading.Thread(
            target=_upload_attachments,
            args=(persona_record["id"], body.attachments),
            daemon=True,
        ).start()

    # Send welcome email with speaker_id (non-blocking)
    threading.Thread(
        target=_send_welcome_email,
        args=(body.email, body.full_name, speaker_id, body.attachments),
        daemon=True,
    ).start()

    # Create profile JSON and trigger first scout run (non-blocking)
    if persona_record:
        persona_record_id = persona_record['id']
        threading.Thread(
            target=_create_profile_and_run_scout,
            args=(speaker_id, body, persona_record_id),
            daemon=True,
        ).start()

    return {
        "speaker_id": speaker_id,
        "id": record["id"],
        **record.get("fields", {}),
    }


@app.get("/api/speaker/{speaker_id}")
def get_speaker(speaker_id: str, _: None = Depends(verify_api_key)):
    """Get speaker profile from Airtable (merges Speakers + Speaker_Persona)."""
    logger.info(f"Fetching speaker {speaker_id} from Airtable")
    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")
    persona = at.get_persona(speaker_id)
    # Merge: persona fields override speaker fields (persona holds the profile details)
    fields = {**record.get("fields", {}), **(persona.get("fields", {}) if persona else {})}
    result = {"id": record["id"], "persona_id": persona["id"] if persona else None, **fields}

    # If speaker has a tier, include plan limits and current scouts used
    tier = (fields.get('Plan') or '').strip()
    if tier in _TIER_MAX_LEADS:
        from datetime import datetime, timedelta
        max_scout_runs = _TIER_MAX_SCOUTS[tier]
        max_leads_per_run = _TIER_MAX_LEADS[tier]
        scouts_used = int(fields.get('scouts_used') or 0)
        reset_date_str = fields.get('scouts_reset_date') or ''
        resets_at = None
        try:
            reset_date = datetime.strptime(reset_date_str[:10], '%Y-%m-%d').date()
            resets_at = (reset_date + timedelta(days=7)).isoformat()
        except (ValueError, TypeError):
            pass
        result['plan'] = {
            'tier': tier,
            'max_scout_runs': max_scout_runs,
            'max_leads_per_run': max_leads_per_run,
            'scouts_used': scouts_used,
            'scouts_remaining': max(0, max_scout_runs - scouts_used),
            'resets_at': resets_at,
        }

    return result


def _fetch_trending_topics(industries: list, credentials: str) -> str:
    """Search for trending conference topics via SerpAPI or Serper. Returns a formatted context string."""
    industry_str = industries[0] if industries else credentials or 'professional development'
    queries = [
        f'trending conference keynote topics {date.today().year} {industry_str}',
        f'most popular speaker topics {industry_str} conferences {date.today().year}',
    ]

    snippets = _fetch_trending_topics_serpapi(queries)
    if not snippets:
        snippets = _fetch_trending_topics_serper(queries)
    if not snippets:
        snippets = _fetch_trending_topics_tavily(queries)

    if not snippets:
        return ''
    return "REAL-WORLD TRENDING TOPICS FROM WEB (use as grounding):\n" + '\n'.join(snippets[:10])


def _fetch_trending_topics_serpapi(queries: list) -> list:
    """Fetch trending topic snippets via SerpAPI. Returns list of formatted strings."""
    serp_key = os.getenv('SERP_API_KEY', '')
    if not serp_key:
        return []

    snippets = []
    for query in queries:
        try:
            resp = http_requests.get(
                'https://serpapi.com/search.json',
                params={'q': query, 'api_key': serp_key, 'num': 10, 'hl': 'en', 'gl': 'us'},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(f"[TOPICS] SerpAPI {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('organic_results', [])[:10]:
                title = r.get('title', '')
                snippet = r.get('snippet', '')
                if title or snippet:
                    snippets.append(f"- {title}: {snippet}")
        except Exception as e:
            logger.warning(f"[TOPICS] SerpAPI failed for '{query}': {e}")

    if snippets:
        logger.info(f"[TOPICS] SerpAPI returned {len(snippets)} snippets")
    return snippets


def _fetch_trending_topics_serper(queries: list) -> list:
    """Fetch trending topic snippets via Serper.dev. Returns list of formatted strings."""
    serper_key = os.getenv('SERPER_API_KEY', '')
    if not serper_key:
        return []

    snippets = []
    for query in queries:
        try:
            resp = http_requests.post(
                'https://google.serper.dev/search',
                headers={'X-API-KEY': serper_key, 'Content-Type': 'application/json'},
                json={'q': query, 'num': 10, 'gl': 'us', 'hl': 'en'},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(f"[TOPICS] Serper {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('organic', [])[:10]:
                title = r.get('title', '')
                snippet = r.get('snippet', '')
                if title or snippet:
                    snippets.append(f"- {title}: {snippet}")
        except Exception as e:
            logger.warning(f"[TOPICS] Serper failed for '{query}': {e}")

    if snippets:
        logger.info(f"[TOPICS] Serper returned {len(snippets)} snippets")
    return snippets


def _fetch_trending_topics_tavily(queries: list) -> list:
    """Fetch trending topic snippets via Tavily. Returns list of formatted strings."""
    tavily_key = os.getenv('TAVILY_API_KEY', '')
    if not tavily_key:
        return []

    snippets = []
    for query in queries:
        try:
            resp = http_requests.post(
                'https://api.tavily.com/search',
                headers={'Content-Type': 'application/json'},
                json={'api_key': tavily_key, 'query': query, 'max_results': 10, 'search_depth': 'basic'},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(f"[TOPICS] Tavily {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('results', [])[:5]:
                title = r.get('title', '')
                content = r.get('content', '')
                if title or content:
                    snippets.append(f"- {title}: {content[:200]}")
        except Exception as e:
            logger.warning(f"[TOPICS] Tavily failed for '{query}': {e}")

    if snippets:
        logger.info(f"[TOPICS] Tavily returned {len(snippets)} snippets")
    return snippets


class TopicSuggestionsRequest(BaseModel):
    persona_id: Optional[str] = None
    personaBio: Optional[str] = None
    personaCredentials: Optional[str] = None
    personaGritFactor: Optional[str] = None
    topics: Optional[List[SpeakerTopic]] = None


@app.post("/api/topics")
def suggest_topics(
    body: TopicSuggestionsRequest,
    speaker_id: str = Query(...),
    _: None = Depends(verify_api_key),
):
    """Generate AI-powered topic suggestions grounded in real-time web trends via SerpAPI."""
    at = get_airtable()

    fields = {}
    if body.persona_id:
        record = at.get_persona_by_id(body.persona_id)
        if not record:
            raise HTTPException(status_code=404, detail="Persona not found")
        fields = record.get('fields', {})

    # Parse existing topics — prefer body.topics when no persona_id
    existing_topics = []
    if body.persona_id:
        raw_topics = fields.get('topics', '')
        if raw_topics:
            try:
                topic_list = json.loads(raw_topics) if isinstance(raw_topics, str) else raw_topics
                existing_topics = [t.get('title', '') for t in topic_list if isinstance(t, dict)]
            except (json.JSONDecodeError, TypeError):
                pass
    elif body.topics:
        existing_topics = [t.title for t in body.topics]

    # Parse target industries
    industries = []
    raw_ind = fields.get('target_industries', '')
    if raw_ind:
        try:
            industries = json.loads(raw_ind) if isinstance(raw_ind, str) else raw_ind
        except (json.JSONDecodeError, TypeError):
            pass

    
    tagline = body.personaGritFactor or fields.get('tagline', '')
    credentials = body.personaCredentials or fields.get('credentials', '')
    bio = ((body.personaBio or fields.get('bio', '') or ''))[:600]
    # years_exp = fields.get('years_experience', '')
    existing_str = ', '.join(existing_topics) if existing_topics else 'None provided'
    industries_str = ', '.join(industries) if industries else 'General'

    logger.info(f"[TOPICS] Generating topics for {speaker_id} with tagline='{tagline}', credentials='{credentials}', industries='{industries_str}', existing_topics='{existing_str}'")

    # Fetch live trending data from SerpAPI
    serp_context = _fetch_trending_topics(industries, credentials)
    logger.info(f"[TOPICS] Web context {'fetched' if serp_context else 'unavailable'} for {speaker_id}")

    prompt = f"""You are a speaking industry expert helping identify high-demand conference topics.

SPEAKER PROFILE:
- Title: {tagline}
- Credentials: {credentials}
- Target Industries: {industries_str}
- Bio: {bio}
- Existing Topics: {existing_str}

{serp_context}

Generate 10 trending, high-demand conference topic suggestions tailored to this speaker's expertise. Topics should be:
1. Grounded in what's actually trending at conferences right now (use the web data above if provided)
2. Different from their existing topics
3. Specific and compelling, not generic
4. Tied to real industry trends the speaker is credibly positioned to speak on

Return ONLY a valid JSON array with exactly 10 objects, each with:
- "title": concise topic name (5-10 words)
- "abstract": 1-2 sentences max (keep brief)
- "audience": who this is best suited for (one line)
- "trend": one sentence on why this topic is in demand right now

Return JSON only, no markdown, no extra text."""

    api_key = os.getenv('CLAUDE_API_KEY', '')
    model = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6')
    if not api_key:
        raise HTTPException(status_code=503, detail="CLAUDE_API_KEY not configured")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=10000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = response.content[0].text.strip()
        logger.info(f"[TOPICS] Claude stop_reason={response.stop_reason}, length={len(text)}")
        # Extract JSON array robustly — find first '[' and last ']'
        start = text.find('[')
        end = text.rfind(']')
        if start == -1 or end == -1:
            logger.error(f"[TOPICS] No JSON array in Claude response: {text[:1000]}")
            raise json.JSONDecodeError("No JSON array found in response", text, 0)
        topics = json.loads(text[start:end + 1])
        result = {"speaker_id": speaker_id, "topics": topics, "web_grounded": bool(serp_context)}
        if body.persona_id:
            result["persona_id"] = body.persona_id
        return result
    except json.JSONDecodeError as e:
        logger.error(f"[TOPICS] Failed to parse Claude response for {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse AI response")
    except Exception as e:
        logger.error(f"[TOPICS] Error generating topics for {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Niche keywords ──────────────────────────────────────────

class NicheKeywordsRequest(BaseModel):
    # speaker_id: Optional[str] = None
    bio: Optional[str] = None
    tagline: Optional[str] = None
    credentials: Optional[str] = None
    topics: Optional[List[str]] = None


@app.post("/api/niche-keywords")
def niche_keywords(body: NicheKeywordsRequest, _: None = Depends(verify_api_key)):
    """Generate niche keyword suggestions for a speaker based on their profile."""
    api_key = os.getenv('CLAUDE_API_KEY', '')
    if not api_key:
        raise HTTPException(status_code=503, detail="CLAUDE_API_KEY not configured")
    

    topics_str = ', '.join(body.topics) if body.topics else 'None provided'

    prompt = f"""You are a speaking industry SEO and positioning expert.

Given the following speaker profile, generate niche keyword phrases that:
1. Precisely describe the speaker's unique expertise and positioning
2. Are specific enough to differentiate them (avoid generic terms like "leadership" or "innovation")
3. Would be used by event organizers searching for speakers on these exact topics
4. Mix long-tail phrases (3-5 words) with niche single terms
5. Include industry-specific jargon and terminology they own

SPEAKER PROFILE:
- Tagline / Grit Factor: {body.tagline or 'Not provided'}
- Credentials: {body.credentials or 'Not provided'}
- Topics: {topics_str}
- Bio: {(body.bio or '')[:800]}

Return ONLY a valid JSON object with these keys:
- "primary_keywords": array of 5 high-priority niche keyword phrases (most specific to their unique angle)

Return JSON only, no markdown, no extra text."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6')
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        text = response.content[0].text.strip()
        start = text.find('{')
        end = text.rfind('}')
        if start == -1 or end == -1:
            logger.error(f"[KEYWORDS] No JSON in Claude response: {text[:500]}")
            raise HTTPException(status_code=500, detail="Failed to parse AI response")
        result = json.loads(text[start:end + 1])
        logger.info(f"[KEYWORDS] Generated niche keywords")
        return result
    except json.JSONDecodeError as e:
        logger.error(f"[KEYWORDS] JSON parse error: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse AI response")
    except Exception as e:
        logger.error(f"[KEYWORDS] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Speaker contents upload ──────────────────────────────────

class SpeakerContentsUploadRequest(BaseModel):
    files: List[EmailAttachment]


@app.post("/api/speaker/{speaker_id}/contents")
def upload_speaker_contents(speaker_id: str, body: SpeakerContentsUploadRequest, _: None = Depends(verify_api_key)):
    """Upload one or more files to the Contents field of a speaker's Airtable record."""
    if not body.files:
        raise HTTPException(status_code=400, detail="No files provided")

    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")

    record_id = record['id']
    field_name = os.getenv('AIRTABLE_CONTENTS_FIELD', 'Contents')

    def _upload(rid: str, files, fname: str):
        _at = get_airtable()
        for f in files:
            try:
                _at.upload_attachment(rid, fname, f.filename, f.content, f.type or 'application/octet-stream')
                logger.info(f"[CONTENTS] Uploaded '{f.filename}' for {speaker_id}")
            except Exception as e:
                logger.error(f"[CONTENTS] Failed to upload '{f.filename}' for {speaker_id}: {e}")

    threading.Thread(
        target=_upload,
        args=(record_id, body.files, field_name),
        daemon=True,
    ).start()

    return {"status": "uploading", "speaker_id": speaker_id, "files": [f.filename for f in body.files]}


@app.put("/api/speaker/{speaker_id}/plan")
def update_speaker_plan(speaker_id: str, _: None = Depends(verify_api_key), tier: str = Query(...)):
    """Update a speaker's plan tier. Resets scouts_used and scouts_reset_date."""
    if tier not in _TIER_MAX_SCOUTS:
        valid = ', '.join(_TIER_MAX_SCOUTS.keys())
        raise HTTPException(status_code=400, detail=f"Invalid tier '{tier}'. Must be one of: {valid}")

    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")

    result = at.update_speaker(record['id'], {
        'Plan': tier,
        'scouts_used': 0,
        'scouts_reset_date': date.today().isoformat(),
    })
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update plan")

    logger.info(f"[PLAN] Speaker {speaker_id} plan updated to {tier}")
    return {
        "speaker_id": speaker_id,
        "tier": tier,
        "max_scout_runs": _TIER_MAX_SCOUTS[tier],
        "max_leads_per_run": _TIER_MAX_LEADS[tier],
        "scouts_used": 0,
        "scouts_reset_date": date.today().isoformat(),
    }


@app.put("/api/speaker/{speaker_id}")
def update_speaker(speaker_id: str, body: SpeakerUpdate, _: None = Depends(verify_api_key)):
    """Update speaker profile. Identity fields → Speakers, profile fields → Speaker_Persona."""
    logger.info(f"Received update for speaker {speaker_id}")
    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")

    record_id = record["id"]

    # Identity fields → Speakers table
    speaker_fields = {}
    if body.full_name is not None:
        speaker_fields['full_name'] = body.full_name
    if body.email is not None:
        speaker_fields['email'] = body.email

    # Profile fields → Speaker_Persona table
    persona_fields = {}
    if body.tagline is not None:
        persona_fields['tagline'] = body.tagline
    if body.bio is not None:
        persona_fields['bio'] = body.bio
    if body.topics is not None:
        persona_fields['topics'] = json.dumps([t.model_dump() for t in body.topics])
    if body.target_industries is not None:
        persona_fields['target_industries'] = json.dumps(body.target_industries)
    if body.min_honorarium is not None:
        persona_fields['min_honorarium'] = body.min_honorarium
    if body.years_experience is not None:
        persona_fields['years_experience'] = body.years_experience
    if body.location is not None:
        persona_fields['location'] = body.location
    if body.website is not None:
        persona_fields['website'] = body.website
    if body.credentials is not None:
        persona_fields['credentials'] = body.credentials
    if body.linkedin is not None:
        persona_fields['linkedin'] = body.linkedin
    if body.speaker_sheet is not None:
        persona_fields['speaker_sheet'] = body.speaker_sheet
    if body.notes is not None:
        persona_fields['notes'] = body.notes
    if body.conference_year is not None:
        persona_fields['conference_year'] = body.conference_year
    if body.conference_tier is not None:
        persona_fields['conference_tier'] = body.conference_tier
    if body.zip_code is not None:
        persona_fields['zip_code'] = body.zip_code

    if not speaker_fields and not persona_fields and not body.attachments:
        return {"id": record_id, **record.get("fields", {})}

    result = record
    if speaker_fields:
        result = at.update_speaker(record_id, speaker_fields)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to update speaker")

    persona_result = None
    persona_record_id = ''
    if persona_fields:
        persona = at.get_persona(speaker_id)
        if persona:
            persona_record_id = persona['id']
            persona_result = at.update_persona(persona_record_id, persona_fields)
        else:
            # Persona row doesn't exist yet — create it
            persona_fields['speaker_id'] = speaker_id
            persona_result = at.create_persona(persona_fields)
            if persona_result:
                persona_record_id = persona_result['id']
        if persona_result:
            merged = {**(result.get("fields", {}) if result else {}),
                      **persona_result.get("fields", {})}
            _rebuild_profile_json(speaker_id, merged, persona_record_id)

    if body.attachments:
        # Resolve persona record ID — prefer already-fetched result, else query Airtable
        persona_rid = (
            persona_result['id'] if persona_result
            else (at.get_persona(speaker_id) or {}).get('id')
        )
        if persona_rid:
            def _upload_attachments(rid: str, attachments):
                field_name = os.getenv('AIRTABLE_ATTACHMENT_FIELD', 'Attachments')
                _at = get_airtable()
                for attachment in attachments:
                    try:
                        _at.upload_attachment(rid, field_name, attachment.filename, attachment.content, attachment.type or 'application/octet-stream')
                    except Exception as e:
                        logger.error(f"Failed to upload attachment '{attachment.filename}': {e}")
            threading.Thread(
                target=_upload_attachments,
                args=(persona_rid, body.attachments),
                daemon=True,
            ).start()
        else:
            logger.error(f"Cannot upload attachments for {speaker_id}: no Speaker_Persona record found")

    # Return merged fields
    merged_fields = {**record.get("fields", {})}
    if result and result != record:
        merged_fields.update(result.get("fields", {}))
    if persona_result:
        merged_fields.update(persona_result.get("fields", {}))
    return {"id": record_id, **merged_fields}


def _rebuild_profile_json(speaker_id: str, fields: dict, persona_record_id: str = ''):
    """Rebuild speaker profile JSON file from merged Speakers + Speaker_Persona fields."""
    from src.api.profile_utils import build_profile_from_fields, save_profile
    try:
        profile = build_profile_from_fields(fields)
        save_profile(speaker_id, profile, persona_record_id)
        logger.info(f"Rebuilt profile JSON for {speaker_id} (persona: {persona_record_id or 'default'})")
    except Exception as e:
        logger.error(f"Failed to rebuild profile JSON for {speaker_id}: {e}")


# ── Dashboard pipeline stats ────────────────────────────────

@app.get("/api/dashboard/{speaker_id}/pipeline-stats")
def pipeline_stats(
    speaker_id: str,
    persona_id: Optional[str] = Query(None, description="Speaker_Persona record ID to scope stats"),
    _: None = Depends(verify_api_key),
):
    """Return the four headline stats for the speaker dashboard UI.

    Returns:
        opportunities_identified: total leads + leads found in last 7 days
        active_pitches: contacted leads + count awaiting response (not yet replied/booked)
        response_rate: (Replied+Booked) / outreach_total, plus month-over-month delta
        revenue_pipeline: min_honorarium × pipeline leads (Contacted+Replied+Booked)
    """
    from datetime import datetime, timedelta

    at = get_airtable()
    all_leads = at.get_leads(speaker_id=speaker_id, persona_id=persona_id or '')

    # Speaker min_honorarium for revenue estimate
    min_fee = 0
    speaker = at.get_speaker(speaker_id)
    if speaker:
        min_fee = int(speaker.get('fields', {}).get('min_honorarium') or 0)

    today = date.today()
    week_ago = today - timedelta(days=7)
    thirty_days_ago = today - timedelta(days=30)
    sixty_days_ago = today - timedelta(days=60)

    # Bucket counts
    total = len(all_leads)
    new_this_week = 0
    contacted = 0
    replied = 0
    booked = 0
    passed = 0

    # Same buckets but only for the last 30 days (response rate current period)
    outreach_last30 = 0
    responded_last30 = 0
    # Prior 30-day window (day -60 to -30) for delta
    outreach_prev30 = 0
    responded_prev30 = 0

    for record in all_leads:
        f = record.get('fields', {})
        status = f.get('Lead Status', 'New')
        date_found_raw = f.get('Date Found', '')

        # Parse Date Found
        lead_date = None
        if date_found_raw:
            try:
                lead_date = datetime.fromisoformat(date_found_raw.replace('Z', '+00:00')).date()
            except (ValueError, TypeError):
                try:
                    lead_date = datetime.strptime(date_found_raw[:10], '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    pass

        if lead_date and lead_date >= week_ago:
            new_this_week += 1

        if status == 'Contacted':
            contacted += 1
        elif status == 'Replied':
            replied += 1
        elif status == 'Booked':
            booked += 1
        elif status == 'Passed':
            passed += 1

        # Response rate windows — based on when lead was found
        if lead_date:
            if lead_date >= thirty_days_ago:
                if status in ('Contacted', 'Replied', 'Booked', 'Passed'):
                    outreach_last30 += 1
                if status in ('Replied', 'Booked'):
                    responded_last30 += 1
            elif lead_date >= sixty_days_ago:
                if status in ('Contacted', 'Replied', 'Booked', 'Passed'):
                    outreach_prev30 += 1
                if status in ('Replied', 'Booked'):
                    responded_prev30 += 1

    # ── Compute stats ────────────────────────────────────────

    # 1. Opportunities identified
    opportunities = {
        "value": total,
        "change_value": new_this_week,
        "change_label": f"+{new_this_week} this week",
        "change_type": "positive" if new_this_week > 0 else "neutral",
    }

    # 2. Active pitches (Contacted = sent, awaiting reply)
    awaiting = contacted  # still in Contacted = no reply yet
    active_pitches = {
        "value": contacted + replied,
        "change_value": awaiting,
        "change_label": f"{awaiting} awaiting response",
        "change_type": "neutral",
    }

    # 3. Response rate
    current_rate = round(responded_last30 / outreach_last30 * 100, 1) if outreach_last30 else 0
    prev_rate = round(responded_prev30 / outreach_prev30 * 100, 1) if outreach_prev30 else 0
    rate_delta = round(current_rate - prev_rate, 1)
    # Fall back to all-time rate if no windowed data
    if outreach_last30 == 0:
        all_outreach = contacted + replied + booked + passed
        current_rate = round((replied + booked) / all_outreach * 100, 1) if all_outreach else 0
        rate_delta = 0
    response_rate = {
        "value": current_rate,
        "value_formatted": f"{current_rate}%",
        "change_value": rate_delta,
        "change_label": (
            f"+{rate_delta}% vs last month" if rate_delta > 0
            else f"{rate_delta}% vs last month" if rate_delta < 0
            else "Same as last month"
        ),
        "change_type": "positive" if rate_delta > 0 else "negative" if rate_delta < 0 else "neutral",
    }

    # 4. Revenue pipeline
    pipeline_leads = contacted + replied + booked
    pipeline_value = min_fee * pipeline_leads
    revenue_pipeline = {
        "value": pipeline_value,
        "value_formatted": f"${pipeline_value:,}",
        "change_value": pipeline_leads,
        "change_label": f"Based on ${min_fee:,} floor" if min_fee else f"{pipeline_leads} active leads",
        "change_type": "neutral",
    }

    return {
        "speaker_id": speaker_id,
        "opportunities_identified": opportunities,
        "active_pitches": active_pitches,
        "response_rate": response_rate,
        "revenue_pipeline": revenue_pipeline,
    }


# ── Dashboard (combined) ────────────────────────────────────

@app.get("/api/dashboard/{speaker_id}")
def dashboard(
    speaker_id: str,
    persona_id: Optional[str] = Query(None, description="Speaker_Persona record ID to scope this dashboard"),
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None, description="Filter by lead type: Conference, Podcast, Corporate Events, Local Events, Other"),
    _: None = Depends(verify_api_key),
):
    """Combined dashboard data: profile + stats + top leads."""
    at = get_airtable()

    # Resolve persona
    if persona_id:
        persona = at.get_persona_by_id(persona_id)
    else:
        persona = at.get_persona(speaker_id)
    resolved_persona_id = persona["id"] if persona else None

    # Stats
    stats = at.get_lead_stats(speaker_id, persona_id=resolved_persona_id or '')

    all_leads = at.get_leads(speaker_id=speaker_id, persona_id=resolved_persona_id or '', status=status or '', lead_type=type or '')
    sorted_leads = sorted(
        all_leads,
        key=lambda r: r.get('fields', {}).get('Match Score', 0),
        reverse=True,
    )
    top_leads = [{"id": r["id"], **r.get("fields", {})} for r in sorted_leads]

    # Speaker profile
    speaker = at.get_speaker(speaker_id)
    speaker_data = None
    if speaker:
        speaker_data = {"id": speaker["id"], **speaker.get("fields", {})}

    return {
        "speaker": speaker_data,
        "persona_id": resolved_persona_id,
        "stats": stats,
        "top_leads": top_leads,
    }


# ── Admin ──────────────────────────────────────────────────

def _check_admin(request: Request):
    """Verify admin password from Authorization header."""
    admin_pw = os.getenv('ADMIN_PASSWORD', '')
    if not admin_pw:
        raise HTTPException(status_code=503, detail="Admin not configured")
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != admin_pw:
        raise HTTPException(status_code=401, detail="Unauthorized")


class AdminLogin(BaseModel):
    password: str


@app.post("/api/admin/login")
def admin_login(body: AdminLogin):
    """Verify admin password."""
    admin_pw = os.getenv('ADMIN_PASSWORD', '')
    if not admin_pw:
        raise HTTPException(status_code=503, detail="Admin not configured")
    if body.password != admin_pw:
        raise HTTPException(status_code=401, detail="Invalid password")
    return {"status": "ok"}


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    """High-level business metrics."""
    _check_admin(request)
    at = get_airtable()

    speakers = at.list_active_speakers()
    all_leads = at.get_leads()

    total_score = 0
    leads_today = 0
    today_str = date.today().isoformat()
    triage_counts = {'RED': 0, 'YELLOW': 0, 'GREEN': 0}

    for r in all_leads:
        f = r.get('fields', {})
        total_score += f.get('Match Score', 0)
        # Date Found is stored as ISO datetime
        df = f.get('Date Found', '')
        if isinstance(df, str) and df.startswith(today_str):
            leads_today += 1
        triage = f.get('Lead Triage', '')
        if triage in triage_counts:
            triage_counts[triage] += 1

    return {
        "total_speakers": len(speakers),
        "total_leads": len(all_leads),
        "avg_score": round(total_score / max(len(all_leads), 1), 1),
        "leads_today": leads_today,
        "triage_breakdown": triage_counts,
    }



@app.get("/api/admin/speakers/{speaker_id}/leads")
def admin_speaker_leads(speaker_id: str, request: Request):
    """Get all leads for a specific speaker (admin view)."""
    _check_admin(request)
    at = get_airtable()

    records = at.get_leads(speaker_id=speaker_id)
    leads = [
        {"id": r["id"], **r.get("fields", {})}
        for r in records
    ]
    # Sort by score desc
    leads.sort(key=lambda l: l.get("Match Score", 0), reverse=True)

    return {"speaker_id": speaker_id, "count": len(leads), "leads": leads}
