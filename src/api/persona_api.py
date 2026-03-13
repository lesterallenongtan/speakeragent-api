"""Speaker Persona API — CRUD for Speaker_Persona table."""

import json
import logging
import os
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.airtable import AirtableAPI
from config.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_airtable() -> AirtableAPI:
    s = Settings()
    return AirtableAPI(
        api_key=s.AIRTABLE_API_KEY,
        base_id=s.AIRTABLE_BASE_ID,
        leads_table=s.LEADS_TABLE,
        speakers_table=s.SPEAKERS_TABLE,
        persona_table=os.getenv('PERSONA_TABLE', 'Speaker_Persona'),
    )


from src.api.deps import verify_api_key, TIER_MAX_PERSONAS


# ── Pydantic models ───────────────────────────────────────────────────────────

class PersonaTopic(BaseModel):
    title: str
    abstract: Optional[str] = ""
    audience: Optional[str] = ""


class PersonaCreate(BaseModel):
    persona_name: Optional[str] = None
    tagline: Optional[str] = None
    bio: Optional[str] = None
    topics: Optional[List[PersonaTopic]] = None
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
    # status: Optional[str] = 'active'


class PersonaUpdate(BaseModel):
    persona_name: Optional[str] = None
    tagline: Optional[str] = None
    bio: Optional[str] = None
    topics: Optional[List[PersonaTopic]] = None
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
    # status: Optional[str] = None


def _run_scout_bg(speaker_id: str, speaker_name: str, persona_record_id: str, body: 'PersonaCreate'):
    """Build profile JSON and trigger scout for a persona (runs in background thread)."""
    from src.api.profile_utils import create_profile_and_run_scout
    create_profile_and_run_scout(
        speaker_id,
        persona_record_id,
        body=body,
        full_name=speaker_name,
    )


def _body_to_fields(body: PersonaCreate | PersonaUpdate) -> dict:
    """Convert request body to Airtable fields dict."""
    fields = {}
    fields['persona_name'] = getattr(body, 'persona_name', None) or 'Core Persona'
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
    if body.zip_code is not None:
        fields['zip_code'] = body.zip_code
    # if body.status is not None:
    #     fields['status'] = body.status
    return fields


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get('/api/speaker/{speaker_id}/personas')
def list_personas(speaker_id: str, _: None = Depends(verify_api_key)):
    """List all personas for a speaker."""
    at = _get_airtable()
    records = at.list_personas(speaker_id)
    return {
        'speaker_id': speaker_id,
        'count': len(records),
        'personas': [{'id': r['id'], **r.get('fields', {})} for r in records],
    }


@router.get('/api/speaker/{speaker_id}/persona/{persona_id}')
def get_persona(speaker_id: str, persona_id: str, _: None = Depends(verify_api_key)):
    """Get a single persona by record ID."""
    at = _get_airtable()
    record = at.get_persona_by_id(persona_id)
    if not record:
        raise HTTPException(status_code=404, detail='Persona not found')
    # Verify it belongs to this speaker
    if record.get('fields', {}).get('speaker_id') != speaker_id:
        raise HTTPException(status_code=403, detail='Persona does not belong to this speaker')
    return {'id': record['id'], **record.get('fields', {})}


@router.post('/api/speaker/{speaker_id}/persona', status_code=201)
def create_persona(speaker_id: str, body: PersonaCreate, _: None = Depends(verify_api_key)):
    """Create an additional persona for a speaker."""
    from datetime import date

    at = _get_airtable()

    # Verify speaker exists
    speaker = at.get_speaker(speaker_id)
    if not speaker:
        raise HTTPException(status_code=404, detail='Speaker not found')

    # Enforce persona limit by plan
    plan = (speaker.get('fields', {}).get('Plan') or 'Free').strip()
    max_personas = TIER_MAX_PERSONAS.get(plan, 1)
    existing = at.list_personas(speaker_id)
    if len(existing) >= max_personas:
        raise HTTPException(
            status_code=403,
            detail=f'Persona limit reached for your {plan} plan ({max_personas} persona{"s" if max_personas > 1 else ""} allowed).',
        )

    fields = _body_to_fields(body)
    fields['speaker_id'] = speaker_id
    fields['created_at'] = date.today().isoformat()
    fields['scout_status'] = 'Running'
    # if 'status' not in fields:
    #     fields['status'] = 'active'

    record = at.create_persona(fields)
    if not record:
        raise HTTPException(status_code=500, detail='Failed to create persona')

    persona_record_id = record['id']
    speaker_name = speaker.get('fields', {}).get('full_name', '')

    threading.Thread(
        target=_run_scout_bg,
        args=(speaker_id, speaker_name, persona_record_id, body),
        daemon=True,
    ).start()

    return {'id': record['id'], **record.get('fields', {})}


@router.patch('/api/speaker/{speaker_id}/persona/{persona_id}')
def update_persona(speaker_id: str, persona_id: str, body: PersonaUpdate, _: None = Depends(verify_api_key)):
    """Update a persona."""
    at = _get_airtable()

    record = at.get_persona_by_id(persona_id)
    if not record:
        raise HTTPException(status_code=404, detail='Persona not found')
    if record.get('fields', {}).get('speaker_id') != speaker_id:
        raise HTTPException(status_code=403, detail='Persona does not belong to this speaker')

    fields = _body_to_fields(body)
    if not fields:
        raise HTTPException(status_code=400, detail='No fields provided to update')

    updated = at.update_persona(persona_id, fields)
    if not updated:
        raise HTTPException(status_code=500, detail='Failed to update persona')

    return {'id': updated['id'], **updated.get('fields', {})}


@router.delete('/api/speaker/{speaker_id}/persona/{persona_id}', status_code=200)
def delete_persona(speaker_id: str, persona_id: str, _: None = Depends(verify_api_key)):
    """Delete a persona."""
    at = _get_airtable()

    record = at.get_persona_by_id(persona_id)
    if not record:
        raise HTTPException(status_code=404, detail='Persona not found')
    if record.get('fields', {}).get('speaker_id') != speaker_id:
        raise HTTPException(status_code=403, detail='Persona does not belong to this speaker')

    # Prevent deleting the only/last persona
    existing = at.list_personas(speaker_id)
    if len(existing) <= 1:
        raise HTTPException(status_code=400, detail='Cannot delete the only persona. Update it instead.')

    success = at.delete_persona(persona_id)
    if not success:
        raise HTTPException(status_code=500, detail='Failed to delete persona')

    from src.api.profile_utils import delete_profile
    delete_profile(speaker_id, persona_id)

    return {'deleted': True, 'persona_id': persona_id}


@router.get('/api/speaker/{speaker_id}/persona/{persona_id}/leads')
def get_persona_leads(speaker_id: str, persona_id: str, _: None = Depends(verify_api_key)):
    """Get all leads for a specific persona, sorted by Match Score desc."""
    at = _get_airtable()

    record = at.get_persona_by_id(persona_id)
    if not record:
        raise HTTPException(status_code=404, detail='Persona not found')
    if record.get('fields', {}).get('speaker_id') != speaker_id:
        raise HTTPException(status_code=403, detail='Persona does not belong to this speaker')

    records = at.get_leads(speaker_id=speaker_id, persona_id=persona_id)
    leads = [{'id': r['id'], **r.get('fields', {})} for r in records]
    leads.sort(key=lambda l: l.get('Match Score', 0), reverse=True)

    return {'speaker_id': speaker_id, 'persona_id': persona_id, 'count': len(leads), 'leads': leads}


@router.post('/api/speaker/{speaker_id}/persona/{persona_id}/scout', status_code=202)
def run_scout_for_persona(speaker_id: str, persona_id: str, _: None = Depends(verify_api_key)):
    """Trigger a scout run for a specific persona."""
    import threading
    at = _get_airtable()

    logger.info(f"Received request to run scout for speaker_id={speaker_id} persona_id={persona_id}")

    record = at.get_persona_by_id(persona_id)
    if not record:
        raise HTTPException(status_code=404, detail='Persona not found')
    if record.get('fields', {}).get('speaker_id') != speaker_id:
        raise HTTPException(status_code=403, detail='Persona does not belong to this speaker')

    # Check scout quota on the Speakers row
    from src.api.dashboard_api import _check_and_reset_plan, _run_scout_for_speaker
    plan_info = _check_and_reset_plan(speaker_id)
    if plan_info is None:
        raise HTTPException(status_code=429, detail='Scout quota exhausted for this billing period')
    _, max_scout_runs, scouts_used, _ = plan_info
    remaining = max_scout_runs - scouts_used
    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail=f'Scout quota exhausted ({scouts_used}/{max_scout_runs} runs used). Resets weekly.',
        )

    profile_path = f'config/speaker_profiles/{speaker_id}_{persona_id}.json'
    logger.info(f"Triggering scout for speaker {speaker_id} persona {persona_id}. Remaining quota: {remaining}. Profile path: {profile_path}")
    threading.Thread(
        target=_run_scout_for_speaker,
        args=(speaker_id, profile_path, persona_id),
        daemon=True,
    ).start()

    return {
        'status': 'started',
        'speaker_id': speaker_id,
        'persona_id': persona_id,
        'scouts_remaining': remaining - 1,
    }
