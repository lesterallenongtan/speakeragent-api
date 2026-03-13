"""Admin API — list endpoints for Speakers, Speaker_Persona, and Conferences."""

import logging
import os

from fastapi import APIRouter, Depends

from src.api.airtable import AirtableAPI
from src.api.deps import verify_api_key
from config.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_airtable() -> AirtableAPI:
    s = Settings()
    return AirtableAPI(
        api_key=s.AIRTABLE_API_KEY,
        base_id=s.AIRTABLE_BASE_ID,
        leads_table=s.LEADS_TABLE,
        speakers_table=s.SPEAKERS_TABLE,
        persona_table=os.getenv('PERSONA_TABLE', 'Speaker_Persona'),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/admin/speakers")
def admin_list_speakers(_: None = Depends(verify_api_key)):
    """List all active speakers."""
    at = _get_airtable()

    records = at.list_active_speakers()
    speakers = []
    for r in records:
        f = r.get('fields', {})
        speakers.append({
            "id": r["id"],
            "speaker_id": f.get('speaker_id', ''),
            "full_name": f.get('full_name', ''),
            "email": f.get('email', ''),
            "plan": f.get('Plan', ''),
            "status": f.get('status', ''),
            "created_at": f.get('created_at', ''),
        })

    return {"count": len(speakers), "speakers": speakers}


