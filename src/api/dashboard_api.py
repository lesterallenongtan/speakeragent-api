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
from fastapi.security import APIKeyHeader
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import Settings
from src.api.airtable import AirtableAPI

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

_api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)

def verify_api_key(key: Optional[str] = Depends(_api_key_header)):
    """Require a valid X-API-Key header on protected endpoints."""
    expected = os.getenv('API_KEY', '')  # read at request time — avoids Railway startup ordering issues
    if not expected:
        raise HTTPException(status_code=503, detail="API_KEY not configured")
    if key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Scheduler ───────────────────────────────────────────────

def _ensure_profile_exists(speaker_id: str, profile_path: str) -> str:
    """Ensure a speaker profile JSON exists. Rebuild from Airtable if missing.

    Returns the (possibly updated) profile_path.
    """
    p = Path(profile_path)
    if p.exists():
        return profile_path

    logger.info(f"[SCOUT] Profile file missing: {profile_path}. Rebuilding from Airtable...")
    try:
        at = get_airtable()
        record = at.get_speaker(speaker_id)
        if not record:
            logger.warning(f"[SCOUT] Speaker {speaker_id} not found in Airtable either!")
            return profile_path  # Will fail in run_scout, but at least we tried

        fields = record.get('fields', {})

        # Parse topics from JSON string stored in Airtable
        topics = []
        raw_topics = fields.get('topics', '')
        if raw_topics:
            try:
                topic_list = json.loads(raw_topics) if isinstance(raw_topics, str) else raw_topics
                for t in topic_list:
                    if isinstance(t, dict):
                        topics.append({
                            'topic': t.get('title', ''),
                            'description': t.get('abstract', ''),
                            'audience': t.get('audience', '')
                        })
                    else:
                        topics.append({'topic': t, 'description': ''})
            except (json.JSONDecodeError, TypeError):
                pass

        # Parse target_industries
        industries = []
        raw_ind = fields.get('target_industries', '')
        if raw_ind:
            try:
                industries = json.loads(raw_ind) if isinstance(raw_ind, str) else raw_ind
            except (json.JSONDecodeError, TypeError):
                pass

        profile = {
            'full_name': fields.get('full_name', speaker_id),
            'credentials': fields.get('credentials', ''),
            'professional_title': fields.get('tagline', ''),
            'years_experience': fields.get('years_experience', 0),
            'book_title': '',
            'topics': topics if topics else [{'topic': 'General', 'description': ''}],
            'target_industries': industries,
            'target_geography': fields.get('location', 'National (US)'),
            'min_honorarium': fields.get('min_honorarium', 0),
            'discussion_points': [t['topic'] for t in topics][:10],
            'linkedin': fields.get('linkedin', ''),
            'website': fields.get('website', ''),
            'speaker_sheet': fields.get('speaker_sheet', ''),
            'notes': fields.get('notes', ''),
            'conference_year': fields.get('conference_year', date.today().year),
            'conference_tier': fields.get('conference_tier', ''),
        }
        if fields.get('bio'):
            profile['bio'] = fields['bio']

        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w') as f:
            json.dump(profile, f, indent=2)
        logger.info(f"[SCOUT] Rebuilt profile for {speaker_id} from Airtable data")

    except Exception as e:
        logger.error(f"[SCOUT] Failed to rebuild profile for {speaker_id}: {e}")

    return profile_path


_TIER_MAX_LEADS = {
    'Free': 3,
    'Starter': 10,
    'Pro': 20,
}


def _check_and_reset_plan(speaker_id: str) -> Optional[tuple]:
    """Check the speaker's weekly scout quota, resetting if 7 days have passed.

    Returns (record, max_scouts, scouts_used) if the speaker can run a scout,
    or None if they have exhausted their weekly quota.
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
        max_scouts = _TIER_MAX_LEADS.get(tier, Settings.MAX_LEADS_PER_RUN)
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

        if scouts_used >= max_scouts:
            logger.info(f"[PLAN] {speaker_id} quota exhausted: {scouts_used}/{max_scouts} (tier={tier})")
            return None

        logger.info(f"[PLAN] {speaker_id} tier={tier} scouts={scouts_used}/{max_scouts} — allowed")
        return record, max_scouts, scouts_used

    except Exception as e:
        logger.warning(f"[PLAN] Plan check failed for {speaker_id}: {e}")
        return None


def _run_scout_for_speaker(speaker_id: str, profile_path: str):
    """Run scout pipeline for a single speaker."""
    try:
        from src.agent.scout import run_scout
        # Ensure profile exists (rebuild from Airtable if container was redeployed)
        profile_path = _ensure_profile_exists(speaker_id, profile_path)

        # Check plan quota (weekly reset included)
        plan = _check_and_reset_plan(speaker_id)
        if plan is None:
            logger.info(f"[SCOUT] Skipping {speaker_id}: weekly quota exhausted or no plan")
            return {'skipped': 'quota_exhausted'}
        record, max_scouts, scouts_used = plan

        logger.info(f"[SCOUT] Starting scout for {speaker_id} with profile {profile_path}")
        summary = run_scout(
            profile_path=profile_path,
            speaker_id=speaker_id,
            max_leads=max_scouts,
        )
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
        at = get_airtable()
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
        )
        active_speakers = at.list_active_speakers()
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
            if not sid:
                continue
            # Use stored profile or fall back to default seed path
            profile_path = f"config/speaker_profiles/{sid}.json"
            _run_scout_for_speaker(sid, profile_path)

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
    attachments: Optional[List[EmailAttachment]] = None


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
def trigger_scout(speaker_id: Optional[str] = Query(None), _: None = Depends(verify_api_key)):
    """Manually trigger a scout run. Optionally for a specific speaker."""
    if speaker_id:
        plan_info = _check_and_reset_plan(speaker_id)
        if plan_info is None:
            raise HTTPException(status_code=429, detail="Scout quota exhausted for this billing period")
        _, max_scouts, scouts_used = plan_info
        remaining = max_scouts - scouts_used
        if remaining <= 0:
            raise HTTPException(
                status_code=429,
                detail=f"Scout quota exhausted ({scouts_used}/{max_scouts} used). Resets weekly.",
            )
        profile_path = f"config/speaker_profiles/{speaker_id}.json"
        thread = threading.Thread(
            target=_run_scout_for_speaker,
            args=(speaker_id, profile_path),
            daemon=True,
        )
        thread.start()
        return {"status": "started", "speaker_id": speaker_id, "scouts_remaining": remaining - 1}
    else:
        thread = threading.Thread(target=_run_daily_scout, daemon=True)
        thread.start()
        return {"status": "started", "message": "Scout running for all active speakers"}


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


def _create_profile_and_run_scout(speaker_id: str, body):
    """Create a speaker profile JSON from registration data and trigger scout."""
    try:
        # Build profile dict matching the format expected by scout pipeline
        topics = []
        if body.topics:
            for t in body.topics:
                topics.append({'topic': t.title, 'description': t.abstract or ''})

        profile = {
            'full_name': body.full_name,
            'credentials': body.credentials or '',
            'professional_title': body.tagline or '',
            'years_experience': body.years_experience or 0,
            'book_title': '',
            'topics': topics if topics else [{'topic': 'General', 'description': ''}],
            'target_industries': body.target_industries or [],
            'target_geography': body.location or 'National (US)',
            'min_honorarium': body.min_honorarium or 0,
        }

        # Build discussion_points from topic title strings for better search queries
        discussion_points = []
        topic_objects = body.topics or []
        for t_obj in topic_objects:
            t_str = t_obj.title
            discussion_points.append(t_str)
            phrase = t_str.split(':')[0].strip()
            if phrase != t_str:
                discussion_points.append(phrase)
        profile['discussion_points'] = discussion_points[:10]

        # Store full bio for context
        if body.bio:
            profile['bio'] = body.bio

        # Save profile JSON
        profile_dir = Path('config/speaker_profiles')
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / f'{speaker_id}.json'
        with open(profile_path, 'w') as f:
            json.dump(profile, f, indent=2)

        logger.info(f"[SCOUT] Profile created for {speaker_id}, triggering scout run")
        _run_scout_for_speaker(speaker_id, str(profile_path))

    except Exception as e:
        logger.error(f"[SCOUT] Failed to create profile / run scout for {speaker_id}: {e}", exc_info=True)


# ── Speaker ─────────────────────────────────────────────────

@app.post("/api/speakers/register")
def register_speaker(body: SpeakerRegistration):
    """Register a new speaker. Generates a unique speaker_id."""
    at = get_airtable()

    # Check for duplicate email
    existing = at.get_speaker_by_email(body.email)
    if existing:
        existing_fields = existing.get('fields', {})
        raise HTTPException(
            status_code=409,
            detail={
                "message": "An account with this email already exists.",
                "speaker_id": existing_fields.get('speaker_id', ''),
            }
        )

    # Generate unique speaker_id: slug from name + short UUID
    name_slug = body.full_name.lower().replace(' ', '_').replace('.', '')
    name_slug = ''.join(c for c in name_slug if c.isalnum() or c == '_')
    short_uuid = uuid.uuid4().hex[:8]
    speaker_id = f"{name_slug}_{short_uuid}"

    # Build speaker record
    fields = {
        'speaker_id': speaker_id,
        'full_name': body.full_name,
        'email': body.email,
        'status': 'active',
        'Plan': 'Free',
        'scouts_used': 0,
        'scouts_reset_date': date.today().isoformat(),
        'created_at': date.today().isoformat(),
    }
    if body.tagline:
        fields['tagline'] = body.tagline
    if body.bio:
        fields['bio'] = body.bio
    if body.topics:
        fields['topics'] = json.dumps([t.model_dump() for t in body.topics])
    if body.target_industries:
        fields['target_industries'] = json.dumps(body.target_industries)
    if body.min_honorarium is not None:
        fields['min_honorarium'] = body.min_honorarium
    if body.years_experience is not None:
        fields['years_experience'] = body.years_experience
    if body.location:
        fields['location'] = body.location
    if body.website:
        fields['website'] = body.website
    if body.credentials:
        fields['credentials'] = body.credentials
    if body.linkedin:
        fields['linkedin'] = body.linkedin
    if body.speaker_sheet:
        fields['speaker_sheet'] = body.speaker_sheet
    if body.notes:
        fields['notes'] = body.notes
    if body.conference_year is not None:
        fields['conference_year'] = body.conference_year
    if body.conference_tier:
        fields['conference_tier'] = body.conference_tier

    record = at.create_speaker(fields)
    if not record:
        raise HTTPException(status_code=500, detail="Failed to create speaker")

    # Upload attachments to Airtable (non-blocking)
    if body.attachments:
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
            args=(record["id"], body.attachments),
            daemon=True,
        ).start()

    # Send welcome email with speaker_id (non-blocking)
    threading.Thread(
        target=_send_welcome_email,
        args=(body.email, body.full_name, speaker_id, body.attachments),
        daemon=True,
    ).start()

    # Trigger first scout run immediately (non-blocking)
    threading.Thread(
        target=_create_profile_and_run_scout,
        args=(speaker_id, body),
        daemon=True,
    ).start()

    return {
        "speaker_id": speaker_id,
        "id": record["id"],
        **record.get("fields", {}),
    }


@app.get("/api/speaker/{speaker_id}")
def get_speaker(speaker_id: str, _: None = Depends(verify_api_key)):
    """Get speaker profile from Airtable."""
    logger.info(f"Fetching speaker {speaker_id} from Airtable")
    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")
    fields = record.get("fields", {})
    result = {"id": record["id"], **fields}

    # If speaker has a tier, include plan limits and current scouts used
    tier = (fields.get('Plan') or '').strip()
    if tier in _TIER_MAX_LEADS:
        from datetime import datetime, timedelta
        max_scouts = _TIER_MAX_LEADS[tier]
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
            'max_scouts': max_scouts,
            'scouts_used': scouts_used,
            'scouts_remaining': max(0, max_scouts - scouts_used),
            'resets_at': resets_at,
        }

    return result


def _fetch_trending_topics(industries: list, credentials: str) -> str:
    """Search for trending conference topics via SerpAPI or Serper. Returns a formatted context string."""
    industry_str = industries[0] if industries else credentials or 'professional development'
    queries = [
        f'trending conference keynote topics 2026 {industry_str}',
        f'most popular speaker topics {industry_str} conferences 2026',
    ]

    snippets = _fetch_trending_topics_serpapi(queries)
    if not snippets:
        snippets = _fetch_trending_topics_serper(queries)

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
                params={'q': query, 'api_key': serp_key, 'num': 5, 'hl': 'en', 'gl': 'us'},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(f"[TOPICS] SerpAPI {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('organic_results', [])[:5]:
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
                json={'q': query, 'num': 5, 'gl': 'us', 'hl': 'en'},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.warning(f"[TOPICS] Serper {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('organic', [])[:5]:
                title = r.get('title', '')
                snippet = r.get('snippet', '')
                if title or snippet:
                    snippets.append(f"- {title}: {snippet}")
        except Exception as e:
            logger.warning(f"[TOPICS] Serper failed for '{query}': {e}")

    if snippets:
        logger.info(f"[TOPICS] Serper returned {len(snippets)} snippets")
    return snippets


@app.get("/api/topics")
def suggest_topics(speaker_id: str = Query(...), _: None = Depends(verify_api_key)):
    """Generate AI-powered topic suggestions grounded in real-time web trends via SerpAPI."""
    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")

    fields = record.get('fields', {})

    # Parse existing topics
    existing_topics = []
    raw_topics = fields.get('topics', '')
    if raw_topics:
        try:
            topic_list = json.loads(raw_topics) if isinstance(raw_topics, str) else raw_topics
            existing_topics = [t.get('title', '') for t in topic_list if isinstance(t, dict)]
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse target industries
    industries = []
    raw_ind = fields.get('target_industries', '')
    if raw_ind:
        try:
            industries = json.loads(raw_ind) if isinstance(raw_ind, str) else raw_ind
        except (json.JSONDecodeError, TypeError):
            pass

    full_name = fields.get('full_name', 'the speaker')
    tagline = fields.get('tagline', '')
    credentials = fields.get('credentials', '')
    bio = (fields.get('bio', '') or '')[:600]
    years_exp = fields.get('years_experience', '')
    existing_str = ', '.join(existing_topics) if existing_topics else 'None provided'
    industries_str = ', '.join(industries) if industries else 'General'

    # Fetch live trending data from SerpAPI
    serp_context = _fetch_trending_topics(industries, credentials)
    logger.info(f"[TOPICS] Web context {'fetched' if serp_context else 'unavailable'} for {speaker_id}")

    prompt = f"""You are a speaking industry expert helping identify high-demand conference topics.

SPEAKER PROFILE:
- Name: {full_name}
- Title: {tagline}
- Credentials: {credentials}
- Years of Experience: {years_exp}
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
        return {"speaker_id": speaker_id, "topics": topics, "web_grounded": bool(serp_context)}
    except json.JSONDecodeError as e:
        logger.error(f"[TOPICS] Failed to parse Claude response for {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse AI response")
    except Exception as e:
        logger.error(f"[TOPICS] Error generating topics for {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/speaker/{speaker_id}")
def update_speaker(speaker_id: str, body: SpeakerUpdate, _: None = Depends(verify_api_key)):
    logger.info(f"Received update for speaker {speaker_id}")
    """Update speaker profile. Only non-None fields are changed."""
    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")

    logger.info(f"Updating speaker {speaker_id} with data: {record}")
    record_id = record["id"]
    fields = {}

    if body.full_name is not None:
        fields['full_name'] = body.full_name
    if body.email is not None:
        fields['email'] = body.email
    if body.tagline is not None:
        fields['tagline'] = body.tagline
    if body.bio is not None:
        fields['bio'] = body.bio
    if body.topics is not None:
        fields['topics'] = json.dumps([t.model_dump() for t in body.topics])
    if body.target_industries is not None:
        fields['target_industries'] = json.dumps(body.target_industries)
    if body.min_honorarium is not None:
        fields['min_honorarium'] = body.min_honorarium
    if body.years_experience is not None:
        fields['years_experience'] = body.years_experience
    if body.location is not None:
        fields['location'] = body.location
    if body.website is not None:
        fields['website'] = body.website
    if body.credentials is not None:
        fields['credentials'] = body.credentials
    if body.linkedin is not None:
        fields['linkedin'] = body.linkedin
    if body.speaker_sheet is not None:
        fields['speaker_sheet'] = body.speaker_sheet
    if body.notes is not None:
        fields['notes'] = body.notes
    if body.conference_year is not None:
        fields['conference_year'] = body.conference_year
    if body.conference_tier is not None:
        fields['conference_tier'] = body.conference_tier
    

    if not fields and not body.attachments:
        return {"id": record_id, **record.get("fields", {})}

    result = record
    if fields:
        result = at.update_speaker(record_id, fields)
        if not result:
            raise HTTPException(status_code=500, detail="Failed to update speaker")
        _rebuild_profile_json(speaker_id, result.get("fields", {}))

    if body.attachments:
        attachment_field_name = os.getenv('AIRTABLE_ATTACHMENT_FIELD', 'Attachments')
        for attachment in body.attachments:
            try:
                at.upload_attachment(record_id, attachment_field_name, attachment.filename, attachment.content, attachment.type or 'application/octet-stream')
            except Exception as e:
                logger.error(f"Failed to upload attachment '{attachment.filename}': {e}")

    return {"id": result["id"], **result.get("fields", {})}


def _rebuild_profile_json(speaker_id: str, fields: dict):
    """Rebuild speaker profile JSON file from Airtable fields."""
    try:
        topics = []
        raw_topics = fields.get('topics', '')
        if raw_topics:
            try:
                topic_list = json.loads(raw_topics) if isinstance(raw_topics, str) else raw_topics
                for t in topic_list:
                    if isinstance(t, dict):
                        topics.append({
                            'topic': t.get('title', ''),
                            'description': t.get('abstract', ''),
                            'audience': t.get('audience', '')
                        })
                    else:
                        topics.append({'topic': t, 'description': ''})
            except (json.JSONDecodeError, TypeError):
                pass

        industries = []
        raw_ind = fields.get('target_industries', '')
        if raw_ind:
            try:
                industries = json.loads(raw_ind) if isinstance(raw_ind, str) else raw_ind
            except (json.JSONDecodeError, TypeError):
                pass

        profile = {
            'full_name': fields.get('full_name', speaker_id),
            'credentials': fields.get('credentials', ''),
            'professional_title': fields.get('tagline', ''),
            'years_experience': fields.get('years_experience', 0),
            'book_title': '',
            'topics': topics if topics else [{'topic': 'General', 'description': ''}],
            'target_industries': industries,
            'target_geography': fields.get('location', 'National (US)'),
            'min_honorarium': fields.get('min_honorarium', 0),
            'discussion_points': [t['topic'] for t in topics][:10],
            'linkedin': fields.get('linkedin', ''),
            'website': fields.get('website', ''),
            'speaker_sheet': fields.get('speaker_sheet', ''),
            'notes': fields.get('notes', ''),
            'conference_year': fields.get('conference_year', date.today().year),
            'conference_tier': fields.get('conference_tier', ''),
        }
        if fields.get('bio'):
            profile['bio'] = fields['bio']

        profile_dir = Path('config/speaker_profiles')
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / f'{speaker_id}.json'
        with open(profile_path, 'w') as f:
            json.dump(profile, f, indent=2)
        logger.info(f"Rebuilt profile JSON for {speaker_id}")
    except Exception as e:
        logger.error(f"Failed to rebuild profile JSON for {speaker_id}: {e}")


# ── Dashboard pipeline stats ────────────────────────────────

@app.get("/api/dashboard/{speaker_id}/pipeline-stats")
def pipeline_stats(speaker_id: str, _: None = Depends(verify_api_key)):
    """Return the four headline stats for the speaker dashboard UI.

    Returns:
        opportunities_identified: total leads + leads found in last 7 days
        active_pitches: contacted leads + count awaiting response (not yet replied/booked)
        response_rate: (Replied+Booked) / outreach_total, plus month-over-month delta
        revenue_pipeline: min_honorarium × pipeline leads (Contacted+Replied+Booked)
    """
    from datetime import datetime, timedelta

    at = get_airtable()
    all_leads = at.get_leads(speaker_id=speaker_id)

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
def dashboard(speaker_id: str, status: Optional[str] = Query(None), _: None = Depends(verify_api_key)):
    """Combined dashboard data: profile + stats + top leads."""
    at = get_airtable()

    # Stats
    stats = at.get_lead_stats(speaker_id)

    # Top 5 leads by score
    all_leads = at.get_leads(speaker_id=speaker_id, status=status or '')
    sorted_leads = sorted(
        all_leads,
        key=lambda r: r.get('fields', {}).get('Match Score', 0),
        reverse=True,
    )
    top_leads = [
        {"id": r["id"], **r.get("fields", {})}
        for r in sorted_leads[:5] 
    ]

    # Speaker profile
    speaker = at.get_speaker(speaker_id)
    speaker_data = None
    if speaker:
        speaker_data = {"id": speaker["id"], **speaker.get("fields", {})}

    return {
        "speaker": speaker_data,
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


@app.get("/api/admin/speakers")
def admin_speakers(request: Request):
    """List all speakers with lead counts and avg scores."""
    _check_admin(request)
    at = get_airtable()

    speakers = at.list_active_speakers()
    result = []
    for s in speakers:
        f = s.get('fields', {})
        sid = f.get('speaker_id', '')
        if not sid:
            continue

        # Get lead stats for this speaker
        stats = at.get_lead_stats(sid)

        result.append({
            "id": s["id"],
            "speaker_id": sid,
            "full_name": f.get('full_name', ''),
            "email": f.get('email', ''),
            "created_at": f.get('created_at', ''),
            "status": f.get('status', ''),
            "lead_count": stats.get('total', 0),
            "avg_score": stats.get('avg_score', 0),
        })

    return {"speakers": result}


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
