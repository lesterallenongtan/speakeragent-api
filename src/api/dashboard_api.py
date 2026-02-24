"""FastAPI dashboard for SpeakerAgent.AI.

Serves endpoints that the frontend (Vercel/Next.js) calls to display leads.
Includes APScheduler for daily scout cron job.
"""

import json
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import List, Optional

import requests as http_requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import Settings
from src.api.airtable import AirtableAPI

logger = logging.getLogger(__name__)


# ── Scheduler ───────────────────────────────────────────────

def _run_scout_for_speaker(speaker_id: str, profile_path: str):
    """Run scout pipeline for a single speaker."""
    import sys
    try:
        from src.agent.scout import run_scout
        print(f"[SCOUT] Starting scout for {speaker_id} with profile {profile_path}", file=sys.stderr, flush=True)
        summary = run_scout(
            profile_path=profile_path,
            speaker_id=speaker_id,
        )
        print(
            f"[SCOUT] Complete for {speaker_id}: "
            f"urls={summary.get('total_urls', 0)} "
            f"scraped={summary.get('scraped', 0)} "
            f"scored={summary.get('scored', 0)} "
            f"pushed={summary.get('pushed', 0)} "
            f"dupes={summary.get('skipped_duplicate', 0)} "
            f"scrape_fail={summary.get('skipped_scrape_fail', 0)} "
            f"score_fail={summary.get('skipped_score_fail', 0)}",
            file=sys.stderr, flush=True,
        )
        return summary
    except Exception as e:
        print(f"[SCOUT] Failed for {speaker_id}: {e}", file=sys.stderr, flush=True)
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


class SpeakerRegistration(BaseModel):
    full_name: str
    email: str
    tagline: Optional[str] = None
    bio: Optional[str] = None
    topics: Optional[List[str]] = None
    target_industries: Optional[List[str]] = None
    min_honorarium: Optional[int] = None
    years_experience: Optional[int] = None
    location: Optional[str] = None
    website: Optional[str] = None


# ── Health ──────────────────────────────────────────────────

@app.get("/health")
def health_check():
    cron_active = _scheduler is not None and _scheduler.running if _scheduler else False
    try:
        at = get_airtable()
        ok = at.health_check()
        return {
            "status": "healthy" if ok else "degraded",
            "airtable": ok,
            "cron_active": cron_active,
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e), "cron_active": cron_active}


# ── Leads ───────────────────────────────────────────────────

@app.get("/api/leads")
def list_leads(
    speaker_id: str = Query(..., description="Speaker ID to filter by"),
    status: Optional[str] = Query(None),
    triage: Optional[str] = Query(None),
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
def lead_stats(speaker_id: str = Query(...)):
    """Aggregated lead statistics for a speaker."""
    at = get_airtable()
    stats = at.get_lead_stats(speaker_id)
    return stats


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: str):
    """Get a single lead by Airtable record ID."""
    at = get_airtable()
    record = at.get_lead_by_id(lead_id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"id": record["id"], **record.get("fields", {})}


@app.put("/api/leads/{lead_id}/status")
def update_lead_status(lead_id: str, body: StatusUpdate):
    """Update lead status (New -> Contacted -> Replied -> Booked -> Passed)."""
    valid = {'New', 'Contacted', 'Replied', 'Booked', 'Passed'}
    if body.status not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(valid)}"
        )
    at = get_airtable()
    result = at.update_lead(lead_id, {'Lead Status': body.status})
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update lead")
    return {"id": result["id"], **result.get("fields", {})}


# ── Scout (manual trigger) ─────────────────────────────────

@app.post("/api/scout/run")
def trigger_scout(speaker_id: Optional[str] = Query(None)):
    """Manually trigger a scout run. Optionally for a specific speaker."""
    if speaker_id:
        profile_path = f"config/speaker_profiles/{speaker_id}.json"
        thread = threading.Thread(
            target=_run_scout_for_speaker,
            args=(speaker_id, profile_path),
            daemon=True,
        )
        thread.start()
        return {"status": "started", "speaker_id": speaker_id}
    else:
        thread = threading.Thread(target=_run_daily_scout, daemon=True)
        thread.start()
        return {"status": "started", "message": "Scout running for all active speakers"}


# ── Email ──────────────────────────────────────────────────

def _send_welcome_email(email: str, full_name: str, speaker_id: str):
    """Send welcome email with speaker_id using Resend API."""
    import sys
    print(f"[EMAIL] Starting welcome email for {speaker_id} to {email}", file=sys.stderr, flush=True)

    resend_key = os.getenv('RESEND_API_KEY', '')
    if not resend_key:
        print(f"[EMAIL] No RESEND_API_KEY set. Skipping welcome email for {speaker_id}", file=sys.stderr, flush=True)
        return

    email_from = os.getenv('EMAIL_FROM', 'SpeakerAgent.AI <onboarding@resend.dev>')
    frontend_url = os.getenv('FRONTEND_URL', 'https://frontend-production-4a8a.up.railway.app')

    try:
        print(f"[EMAIL] Sending via Resend from={email_from} to={email}", file=sys.stderr, flush=True)
        resp = http_requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {resend_key}',
                'Content-Type': 'application/json',
            },
            json={
                'from': email_from,
                'to': [email],
                'subject': f'Welcome to SpeakerAgent.AI — Your Speaker ID',
                'html': (
                    f'<h2>Welcome to SpeakerAgent.AI, {full_name}!</h2>'
                    f'<p>Your account has been created successfully. Here is your Speaker ID:</p>'
                    f'<div style="background:#f0f4f8;padding:16px 24px;border-radius:8px;text-align:center;margin:24px 0;">'
                    f'<code style="font-size:24px;font-weight:bold;color:#1e40af;">{speaker_id}</code>'
                    f'</div>'
                    f'<p>Use this ID to log in to your dashboard at any time:</p>'
                    f'<p><a href="{frontend_url}/login" style="color:#2563eb;">Open Your Dashboard</a></p>'
                    f'<p>Our AI Scout is now being configured to find speaking engagements matched to your profile. '
                    f'You\'ll start seeing leads in your dashboard soon!</p>'
                    f'<br><p>— The SpeakerAgent.AI Team</p>'
                ),
            },
            timeout=10,
        )
        print(f"[EMAIL] Resend response: {resp.status_code} {resp.text}", file=sys.stderr, flush=True)
        if resp.status_code in (200, 201):
            logger.info(f"[EMAIL] Welcome email sent to {email} for {speaker_id}")
        else:
            logger.error(f"[EMAIL] Failed to send: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[EMAIL] Error: {e}", file=sys.stderr, flush=True)
        logger.error(f"[EMAIL] Error sending welcome email: {e}")


def _create_profile_and_run_scout(speaker_id: str, body):
    """Create a speaker profile JSON from registration data and trigger scout."""
    try:
        # Build profile dict matching the format expected by scout pipeline
        topics = []
        if body.topics:
            for t in body.topics:
                topics.append({'topic': t, 'description': ''})

        profile = {
            'full_name': body.full_name,
            'credentials': '',
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
        topic_strings = body.topics or []
        for t_str in topic_strings:
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
        'created_at': date.today().isoformat(),
    }
    if body.tagline:
        fields['tagline'] = body.tagline
    if body.bio:
        fields['bio'] = body.bio
    if body.topics:
        fields['topics'] = json.dumps(body.topics)
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

    record = at.create_speaker(fields)
    if not record:
        raise HTTPException(status_code=500, detail="Failed to create speaker")

    # Send welcome email with speaker_id (non-blocking)
    threading.Thread(
        target=_send_welcome_email,
        args=(body.email, body.full_name, speaker_id),
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
def get_speaker(speaker_id: str):
    """Get speaker profile from Airtable."""
    at = get_airtable()
    record = at.get_speaker(speaker_id)
    if not record:
        raise HTTPException(status_code=404, detail="Speaker not found")
    return {"id": record["id"], **record.get("fields", {})}


# ── Dashboard (combined) ────────────────────────────────────

@app.get("/api/dashboard/{speaker_id}")
def dashboard(speaker_id: str):
    """Combined dashboard data: profile + stats + top leads."""
    at = get_airtable()

    # Stats
    stats = at.get_lead_stats(speaker_id)

    # Top 5 leads by score
    all_leads = at.get_leads(speaker_id=speaker_id)
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
