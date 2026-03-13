"""Contact Card API — CRUD for the Contacts table."""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config.settings import Settings
from src.api.airtable import AirtableAPI
from src.api.deps import verify_api_key

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
        contacts_table=s.CONTACTS_TABLE,
    )


# ── Pydantic models ───────────────────────────────────────────────────────────

class ContactCreate(BaseModel):
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    website_url: Optional[str] = None
    role_title: Optional[str] = None
    organization: Optional[str] = None
    contact_type: Optional[str] = None   # Conference Organizer | Podcast Host | Corporate Events | Media | Other
    status: Optional[str] = 'Not Contacted'
    last_contacted: Optional[str] = None  # YYYY-MM-DD
    notes: Optional[str] = None
    lead_id: Optional[str] = None        # Airtable record ID of the source lead


class ContactUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    website_url: Optional[str] = None
    role_title: Optional[str] = None
    organization: Optional[str] = None
    contact_type: Optional[str] = None
    status: Optional[str] = None
    last_contacted: Optional[str] = None
    notes: Optional[str] = None
    lead_id: Optional[str] = None


VALID_CONTACT_STATUSES = {'Not Contacted', 'Reached Out', 'Responded', 'Booked', 'Declined'}
VALID_CONTACT_TYPES = {'Conference Organizer', 'Podcast Host', 'Corporate Events', 'Media', 'Other'}


def _body_to_fields(body: ContactCreate | ContactUpdate) -> dict:
    """Convert request body to Airtable fields dict."""
    fields = {}
    for attr in (
        'full_name', 'email', 'phone', 'linkedin_url', 'website_url',
        'role_title', 'organization', 'contact_type', 'status',
        'last_contacted', 'notes', 'lead_id',
    ):
        value = getattr(body, attr, None)
        if value is not None:
            fields[attr] = value
    return fields


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post('/api/speaker/{speaker_id}/contacts', status_code=201)
def create_contact(speaker_id: str, body: ContactCreate, _: None = Depends(verify_api_key)):
    """Create a contact card for a speaker."""
    if body.status and body.status not in VALID_CONTACT_STATUSES:
        raise HTTPException(status_code=400, detail=f'Invalid status. Must be one of: {", ".join(sorted(VALID_CONTACT_STATUSES))}')
    if body.contact_type and body.contact_type not in VALID_CONTACT_TYPES:
        raise HTTPException(status_code=400, detail=f'Invalid contact_type. Must be one of: {", ".join(sorted(VALID_CONTACT_TYPES))}')

    at = _get_airtable()

    # Deduplicate by email
    if body.email and at.contact_exists(speaker_id, body.email):
        raise HTTPException(status_code=409, detail='A contact with this email already exists for this speaker')

    fields = _body_to_fields(body)
    fields['speaker_id'] = speaker_id
    fields['date_added'] = date.today().isoformat()

    record = at.create_contact(fields)
    if not record:
        raise HTTPException(status_code=500, detail='Failed to create contact')

    return {'id': record['id'], **record.get('fields', {})}


@router.get('/api/speaker/{speaker_id}/contacts')
def list_contacts(speaker_id: str, _: None = Depends(verify_api_key)):
    """List all contact cards for a speaker."""
    at = _get_airtable()
    records = at.get_contacts(speaker_id)
    return {
        'speaker_id': speaker_id,
        'count': len(records),
        'contacts': [{'id': r['id'], **r.get('fields', {})} for r in records],
    }


@router.get('/api/contacts/{contact_id}')
def get_contact(contact_id: str, _: None = Depends(verify_api_key)):
    """Get a single contact card by record ID."""
    at = _get_airtable()
    record = at.get_contact_by_id(contact_id)
    if not record:
        raise HTTPException(status_code=404, detail='Contact not found')
    return {'id': record['id'], **record.get('fields', {})}


@router.patch('/api/contacts/{contact_id}')
def update_contact(contact_id: str, body: ContactUpdate, _: None = Depends(verify_api_key)):
    """Update a contact card."""
    if body.status and body.status not in VALID_CONTACT_STATUSES:
        raise HTTPException(status_code=400, detail=f'Invalid status. Must be one of: {", ".join(sorted(VALID_CONTACT_STATUSES))}')
    if body.contact_type and body.contact_type not in VALID_CONTACT_TYPES:
        raise HTTPException(status_code=400, detail=f'Invalid contact_type. Must be one of: {", ".join(sorted(VALID_CONTACT_TYPES))}')

    at = _get_airtable()
    record = at.get_contact_by_id(contact_id)
    if not record:
        raise HTTPException(status_code=404, detail='Contact not found')

    fields = _body_to_fields(body)
    if not fields:
        raise HTTPException(status_code=400, detail='No fields provided to update')

    updated = at.update_contact(contact_id, fields)
    if not updated:
        raise HTTPException(status_code=500, detail='Failed to update contact')

    return {'id': updated['id'], **updated.get('fields', {})}


@router.delete('/api/contacts/{contact_id}', status_code=200)
def delete_contact(contact_id: str, _: None = Depends(verify_api_key)):
    """Delete a contact card."""
    at = _get_airtable()
    record = at.get_contact_by_id(contact_id)
    if not record:
        raise HTTPException(status_code=404, detail='Contact not found')

    success = at.delete_contact(contact_id)
    if not success:
        raise HTTPException(status_code=500, detail='Failed to delete contact')

    return {'deleted': True, 'contact_id': contact_id}
