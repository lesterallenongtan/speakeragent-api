"""Airtable REST API client for SpeakerAgent.AI.

Uses direct REST API calls with Personal Access Token.
Handles deduplication, payload cleaning, and field validation.
"""

import logging
import requests
from datetime import date
from typing import List, Optional

logger = logging.getLogger(__name__)


def clean_payload(fields: dict) -> dict:
    """Remove empty/None fields to prevent Airtable 422 errors.

    - Strips None, empty strings, 'TBD', 'N/A'
    - Ensures Match Score is an integer
    - Ensures dateTime fields are ISO 8601 (YYYY-MM-DDTHH:MM:SS.000Z)
    """
    cleaned = {}
    # These Airtable fields are dateTime type — need ISO 8601 with time
    datetime_fields = {'Date Found', 'Event Date', 'created_at', 'date_added', 'last_contacted'}
    number_fields = {'Match Score', 'years_experience', 'min_honorarium', 'scouts_used'}

    for key, value in fields.items():
        if value is None or value == '' or value == 'TBD' or value == 'N/A':
            continue
        if key in number_fields:
            if not isinstance(value, (int, float)):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    continue
            else:
                value = int(value)
        if key in datetime_fields:
            if not isinstance(value, str):
                continue
            # Convert YYYY-MM-DD to full ISO 8601 datetime
            if len(value) == 10 and value[4] == '-' and value[7] == '-':
                value = f'{value}T00:00:00.000Z'
            elif 'T' not in value:
                continue  # Skip invalid date formats
        cleaned[key] = value
    return cleaned


class AirtableAPI:
    """Client for Airtable REST API v0."""

    def __init__(self, api_key: str, base_id: str,
                 leads_table: str = 'Conferences',
                 speakers_table: str = 'Speakers',
                 persona_table: str = 'Speaker_Persona',
                 contacts_table: str = 'Contacts'):
        self.api_key = api_key
        self.base_id = base_id
        self.leads_table = leads_table
        self.speakers_table = speakers_table
        self.persona_table = persona_table
        self.contacts_table = contacts_table
        self.base_url = f'https://api.airtable.com/v0/{base_id}'
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }

    def health_check(self) -> bool:
        """Verify connection to Airtable by listing records."""
        try:
            resp = requests.get(
                f'{self.base_url}/{self.leads_table}',
                headers=self.headers,
                params={'pageSize': 1},
                timeout=10
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Airtable health check failed: {e}")
            return False

    # ── Leads (Conferences table) ────────────────────────────────

    def lead_exists(self, speaker_id: str, conference_name: str) -> bool:
        """Check if a lead already exists (deduplication)."""
        safe_name = conference_name.replace("'", "\\'")
        formula = (
            f"AND({{speaker_id}} = '{speaker_id}', "
            f"{{Conference Name}} = '{safe_name}')"
        )
        params = {'filterByFormula': formula, 'pageSize': 1}
        try:
            resp = requests.get(
                f'{self.base_url}/{self.leads_table}',
                headers=self.headers,
                params=params,
                timeout=10
            )
            resp.raise_for_status()
            records = resp.json().get('records', [])
            return len(records) > 0
        except Exception as e:
            logger.error(f"Dedup check failed: {e}")
            return False  # Proceed with insert on failure

    def push_lead(self, lead: dict) -> Optional[dict]:
        """Push a single lead to the Conferences table.

        Handles deduplication and payload cleaning.
        Returns the created record or None on failure.
        """
        speaker_id = lead.get('speaker_id', '')
        conf_name = lead.get('Conference Name', '')

        if speaker_id and conf_name and self.lead_exists(speaker_id, conf_name):
            logger.info(f"Skipping duplicate: {conf_name}")
            return None

        # Ensure required defaults
        if 'Date Found' not in lead:
            lead['Date Found'] = date.today().isoformat()
        if 'Lead Status' not in lead:
            lead['Lead Status'] = 'New'

        payload = {'fields': clean_payload(lead)}
        try:
            resp = requests.post(
                f'{self.base_url}/{self.leads_table}',
                headers=self.headers,
                json=payload,
                timeout=15
            )
            if resp.status_code == 422:
                logger.error(
                    f"Airtable 422 for '{conf_name}': {resp.text}\n"
                    f"Payload sent: {payload}"
                )
                return None
            resp.raise_for_status()
            record = resp.json()
            logger.info(f"Pushed lead: {conf_name} (id={record.get('id')})")
            return record
        except Exception as e:
            logger.error(f"Failed to push lead '{conf_name}': {e}")
            return None

    def get_leads(self, speaker_id: str = '',
                  status: str = '', triage: str = '', lead_type: str = '',
                  persona_id: str = '') -> list:
        """Fetch leads with optional filters."""
        filters = []
        if speaker_id:
            filters.append(f"{{speaker_id}} = '{speaker_id}'")
        if persona_id:
            filters.append(f"{{persona_id}} = '{persona_id}'")
        if status:
            filters.append(f"{{Lead Status}} = '{status}'")
        if triage:
            filters.append(f"{{Lead Triage}} = '{triage}'")
        if lead_type:
            filters.append(f"{{Type}} = '{lead_type}'")

        params = {}
        if filters:
            if len(filters) == 1:
                params['filterByFormula'] = filters[0]
            else:
                params['filterByFormula'] = f"AND({', '.join(filters)})"

        all_records = []
        offset = None
        while True:
            if offset:
                params['offset'] = offset
            try:
                resp = requests.get(
                    f'{self.base_url}/{self.leads_table}',
                    headers=self.headers,
                    params=params,
                    timeout=15
                )
                resp.raise_for_status()
                data = resp.json()
                all_records.extend(data.get('records', []))
                offset = data.get('offset')
                if not offset:
                    break
            except Exception as e:
                logger.error(f"Failed to fetch leads: {e}")
                break
        return all_records

    def get_lead_by_id(self, record_id: str) -> Optional[dict]:
        """Fetch a single lead by Airtable record ID."""
        try:
            resp = requests.get(
                f'{self.base_url}/{self.leads_table}/{record_id}',
                headers=self.headers,
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch lead {record_id}: {e}")
            return None

    def update_lead(self, record_id: str, fields: dict) -> Optional[dict]:
        """Update a lead record."""
        payload = {'fields': clean_payload(fields)}
        try:
            resp = requests.patch(
                f'{self.base_url}/{self.leads_table}/{record_id}',
                headers=self.headers,
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to update lead {record_id}: {e}")
            return None

    def get_lead_stats(self, speaker_id: str, persona_id: str = '') -> dict:
        """Get aggregated stats for a speaker's leads."""
        leads = self.get_leads(speaker_id=speaker_id, persona_id=persona_id)
        stats = {
            'total': len(leads),
            'by_triage': {'RED': 0, 'YELLOW': 0, 'GREEN': 0},
            'by_status': {},
            'avg_score': 0,
        }
        total_score = 0
        for record in leads:
            fields = record.get('fields', {})
            triage = fields.get('Lead Triage', '')
            if triage in stats['by_triage']:
                stats['by_triage'][triage] += 1
            status = fields.get('Lead Status', 'Unknown')
            stats['by_status'][status] = stats['by_status'].get(status, 0) + 1
            total_score += fields.get('Match Score', 0)
        if stats['total'] > 0:
            stats['avg_score'] = round(total_score / stats['total'], 1)
        return stats

    # ── Speakers table ───────────────────────────────────────────

    def get_speaker(self, speaker_id: str) -> Optional[dict]:
        """Fetch a speaker profile by speaker_id."""
        params = {
            'filterByFormula': f"{{speaker_id}} = '{speaker_id}'",
            'pageSize': 1
        }
        try:
            resp = requests.get(
                f'{self.base_url}/{self.speakers_table}',
                headers=self.headers,
                params=params,
                timeout=10
            )
            resp.raise_for_status()
            records = resp.json().get('records', [])
            return records[0] if records else None
        except Exception as e:
            logger.error(f"Failed to fetch speaker {speaker_id}: {e}")
            return None

    def create_speaker(self, fields: dict) -> Optional[dict]:
        """Create a speaker record in the Speakers table."""
        payload = {'fields': clean_payload(fields)}
        try:
            resp = requests.post(
                f'{self.base_url}/{self.speakers_table}',
                headers=self.headers,
                json=payload,
                timeout=10
            )
            if resp.status_code == 422:
                logger.error(f"Airtable 422 creating speaker: {resp.text}")
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to create speaker: {e}")
            return None

    def get_onboarding_checklist(self, speaker_id: str) -> list:
        """Fetch all checklist rows for a speaker from Onboarding_Checklist."""
        table = 'Onboarding_Checklist'
        params = {
            'filterByFormula': f"{{speaker_id}} = '{speaker_id}'",
            'sort[0][field]': 'Order',
            'sort[0][direction]': 'asc',
        }
        try:
            resp = requests.get(
                f'{self.base_url}/{table}',
                headers=self.headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            records = resp.json().get('records', [])
            return [
                {
                    'id': r['id'],
                    'task': r['fields'].get('Task', ''),
                    'status': r['fields'].get('Status', 'Incomplete'),
                    'order': r['fields'].get('Order'),
                }
                for r in records
            ]
        except Exception as e:
            logger.error(f"[CHECKLIST] Failed to fetch checklist for {speaker_id}: {e}")
            return []

    def complete_checklist_task(self, speaker_id: str, task: str) -> bool:
        """Set a checklist task to Complete for a speaker."""
        table = 'Onboarding_Checklist'
        params = {
            'filterByFormula': f"AND({{speaker_id}} = '{speaker_id}', {{Task}} = '{task}')",
            'pageSize': 1,
        }
        try:
            resp = requests.get(
                f'{self.base_url}/{table}',
                headers=self.headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            records = resp.json().get('records', [])
            if not records:
                logger.warning(f"[CHECKLIST] Task '{task}' not found for {speaker_id}")
                return False
            record_id = records[0]['id']
            patch = requests.patch(
                f'{self.base_url}/{table}/{record_id}',
                headers=self.headers,
                json={'fields': {'Status': 'Completed'}},
                timeout=10,
            )
            if not patch.ok:
                logger.error(f"[CHECKLIST] Failed to complete task '{task}' for {speaker_id}: {patch.status_code} {patch.text[:300]}")
                return False
            logger.info(f"[CHECKLIST] Task '{task}' marked Complete for {speaker_id}")
            return True
        except Exception as e:
            logger.error(f"[CHECKLIST] Exception completing task '{task}' for {speaker_id}: {e}")
            return False

    def create_onboarding_checklist(self, speaker_id: str) -> bool:
        """Insert onboarding checklist rows for a newly registered speaker."""
        tasks = [
            'Update Persona',
            'Run Scout',
            'Review Leads',
            'Approve Lead',
        ]
        table = 'Onboarding_Checklist'
        records = [
            {'fields': {'speaker_id': speaker_id, 'Task': task, 'Status': 'Incomplete', 'Order': idx+1}}
            for idx, task in enumerate(tasks)
        ]
        try:
            resp = requests.post(
                f'{self.base_url}/{table}',
                headers=self.headers,
                json={'records': records},
                timeout=10,
            )
            if not resp.ok:
                logger.error(f"[CHECKLIST] Failed to create checklist for {speaker_id}: {resp.status_code} {resp.text[:300]}")
                return False
            logger.info(f"[CHECKLIST] Created {len(tasks)} checklist items for {speaker_id}")
            return True
        except Exception as e:
            logger.error(f"[CHECKLIST] Exception creating checklist for {speaker_id}: {e}")
            return False

    def get_speaker_by_email(self, email: str) -> Optional[dict]:
        """Fetch a speaker record by email address."""
        params = {
            'filterByFormula': f"{{email}} = '{email}'",
            'pageSize': 1
        }
        try:
            resp = requests.get(
                f'{self.base_url}/{self.speakers_table}',
                headers=self.headers,
                params=params,
                timeout=10
            )
            resp.raise_for_status()
            records = resp.json().get('records', [])
            return records[0] if records else None
        except Exception as e:
            logger.error(f"Failed to fetch speaker by email {email}: {e}")
            return None

    def list_speakers_by_email(self, email: str) -> list:
        """Return all speaker records for a given email address."""
        params = {'filterByFormula': f"{{email}} = '{email}'"}
        try:
            resp = requests.get(
                f'{self.base_url}/{self.speakers_table}',
                headers=self.headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get('records', [])
        except Exception as e:
            logger.error(f"Failed to list speakers by email {email}: {e}")
            return []

    def speaker_exists(self, speaker_id: str) -> bool:
        """Check if a speaker_id is already taken."""
        return self.get_speaker(speaker_id) is not None

    def list_active_speakers(self) -> List[dict]:
        """Get all speakers with status='active'."""
        params = {
            'filterByFormula': "{status} = 'active'",
        }
        all_records = []
        offset = None
        while True:
            if offset:
                params['offset'] = offset
            try:
                resp = requests.get(
                    f'{self.base_url}/{self.speakers_table}',
                    headers=self.headers,
                    params=params,
                    timeout=15
                )
                resp.raise_for_status()
                data = resp.json()
                all_records.extend(data.get('records', []))
                offset = data.get('offset')
                if not offset:
                    break
            except Exception as e:
                logger.error(f"Failed to list active speakers: {e}")
                break
        return all_records

    def update_speaker(self, record_id: str, fields: dict) -> Optional[dict]:
        """Update a speaker record."""
        payload = {'fields': clean_payload(fields)}
        try:
            resp = requests.patch(
                f'{self.base_url}/{self.speakers_table}/{record_id}',
                headers=self.headers,
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            try:
                logger.error(f"Failed to update speaker {record_id}: {e} | response: {resp.text}")
            except Exception:
                logger.error(f"Failed to update speaker {record_id}: {e}")
            return None

    def delete_speaker(self, record_id: str) -> bool:
        """Delete a speaker record by Airtable record ID."""
        try:
            resp = requests.delete(
                f'{self.base_url}/{self.speakers_table}/{record_id}',
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to delete speaker {record_id}: {e}")
            return False

    # ── Speaker_Persona table ─────────────────────────────────

    def list_personas(self, speaker_id: str) -> List[dict]:
        """Fetch ALL persona records for a speaker_id."""
        params = {'filterByFormula': f"{{speaker_id}} = '{speaker_id}'"}
        all_records = []
        offset = None
        while True:
            if offset:
                params['offset'] = offset
            try:
                resp = requests.get(
                    f'{self.base_url}/{self.persona_table}',
                    headers=self.headers,
                    params=params,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                all_records.extend(data.get('records', []))
                offset = data.get('offset')
                if not offset:
                    break
            except Exception as e:
                logger.error(f"Failed to list personas for {speaker_id}: {e}")
                break
        return all_records

    def get_persona_by_id(self, persona_id: str) -> Optional[dict]:
        """Fetch a single persona record by Airtable record ID."""
        try:
            resp = requests.get(
                f'{self.base_url}/{self.persona_table}/{persona_id}',
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch persona {persona_id}: {e}")
            return None

    def get_persona(self, speaker_id: str) -> Optional[dict]:
        """Fetch a persona record by speaker_id."""
        params = {
            'filterByFormula': f"{{speaker_id}} = '{speaker_id}'",
            'pageSize': 1,
        }
        try:
            resp = requests.get(
                f'{self.base_url}/{self.persona_table}',
                headers=self.headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            records = resp.json().get('records', [])
            return records[0] if records else None
        except Exception as e:
            logger.error(f"Failed to fetch persona {speaker_id}: {e}")
            return None

    def create_persona(self, fields: dict) -> Optional[dict]:
        """Create a persona record in Speaker_Persona table."""
        payload = {'fields': clean_payload(fields)}
        logger.info(f"Creating persona with fields: {payload['fields']}")
        try:
            resp = requests.post(
                f'{self.base_url}/{self.persona_table}',
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 422:
                logger.error(f"Airtable 422 creating persona: {resp.text}")
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to create persona: {e}")
            return None

    def update_persona(self, record_id: str, fields: dict) -> Optional[dict]:
        """Update a persona record."""
        payload = {'fields': clean_payload(fields)}
        try:
            resp = requests.patch(
                f'{self.base_url}/{self.persona_table}/{record_id}',
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            try:
                logger.error(f"Failed to update persona {record_id}: {e} | response: {resp.text}")
            except Exception:
                logger.error(f"Failed to update persona {record_id}: {e}")
            return None

    def delete_persona(self, record_id: str) -> bool:
        """Delete a persona record."""
        try:
            resp = requests.delete(
                f'{self.base_url}/{self.persona_table}/{record_id}',
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to delete persona {record_id}: {e}")
            return False

    def list_active_personas(self) -> List[dict]:
        """Get all personas with status='active'."""
        params = {'filterByFormula': "{status} = 'active'"}
        all_records = []
        offset = None
        while True:
            if offset:
                params['offset'] = offset
            try:
                resp = requests.get(
                    f'{self.base_url}/{self.persona_table}',
                    headers=self.headers,
                    params=params,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                all_records.extend(data.get('records', []))
                offset = data.get('offset')
                if not offset:
                    break
            except Exception as e:
                logger.error(f"Failed to list active personas: {e}")
                break
        return all_records

    def get_attachment_field_id(self, table_name: str, field_name: str) -> Optional[str]:
        """Return the Airtable field ID for an attachment field by table/field name."""
        try:
            resp = requests.get(
                f'https://api.airtable.com/v0/meta/bases/{self.base_id}/tables',
                headers=self.headers,
                timeout=10,
            )
            logger.info(f"[ATTACH] Meta API response: {resp.status_code}")
            resp.raise_for_status()
            tables = resp.json().get('tables', [])
            logger.info(f"[ATTACH] Available tables: {[t['name'] for t in tables]}")
            for table in tables:
                if table['name'] == table_name:
                    fields = {f['name']: f['id'] for f in table.get('fields', [])}
                    logger.info(f"[ATTACH] Fields in '{table_name}': {list(fields.keys())}")
                    field_id = fields.get(field_name)
                    if field_id:
                        logger.info(f"[ATTACH] Field '{field_name}' -> {field_id}")
                    else:
                        logger.error(f"[ATTACH] Field '{field_name}' not found in '{table_name}'")
                    return field_id
            logger.error(f"[ATTACH] Table '{table_name}' not found in base")
        except Exception as e:
            logger.error(f"[ATTACH] Meta lookup failed for {table_name}.{field_name}: {e}")
        return None

    def upload_attachment(self, record_id: str, field_name_or_id: str,
                          filename: str, content_b64: str,
                          mime_type: str = 'application/octet-stream') -> bool:
        """Upload an attachment to an Airtable record via the Content API.

        Endpoint: POST https://content.airtable.com/v0/{baseId}/{recordId}/{fieldIdOrName}/uploadAttachment
        Body: JSON with base64-encoded file content.
        """
        url = f'https://content.airtable.com/v0/{self.base_id}/{record_id}/{field_name_or_id}/uploadAttachment'
        logger.info(f"[ATTACH] Uploading '{filename}' ({mime_type}) to {url}")
        try:
            resp = requests.post(
                url,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                },
                json={
                    'contentType': mime_type,
                    'file': content_b64,
                    'filename': filename,
                },
                timeout=30,
            )
            logger.info(f"[ATTACH] Upload response: {resp.status_code} {resp.text[:300]}")
            resp.raise_for_status()
            logger.info(f"[ATTACH] Uploaded '{filename}' to record {record_id}")
            return True
        except Exception as e:
            logger.error(f"[ATTACH] Failed to upload '{filename}' to {record_id}: {e}")
            return False

    # ── Contacts table ────────────────────────────────────────────

    def contact_exists(self, speaker_id: str, email: str) -> bool:
        """Check if a contact with this email already exists for the speaker."""
        safe_email = email.replace("'", "\\'")
        formula = f"AND({{speaker_id}} = '{speaker_id}', {{email}} = '{safe_email}')"
        try:
            resp = requests.get(
                f'{self.base_url}/{self.contacts_table}',
                headers=self.headers,
                params={'filterByFormula': formula, 'pageSize': 1},
                timeout=10,
            )
            resp.raise_for_status()
            return len(resp.json().get('records', [])) > 0
        except Exception as e:
            logger.error(f"Contact dedup check failed: {e}")
            return False

    def create_contact(self, fields: dict) -> Optional[dict]:
        """Create a contact record in the Contacts table."""
        payload = {'fields': clean_payload(fields)}
        try:
            resp = requests.post(
                f'{self.base_url}/{self.contacts_table}',
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 422:
                logger.error(f"Airtable 422 creating contact: {resp.text}")
                return None
            resp.raise_for_status()
            record = resp.json()
            logger.info(f"Created contact: {fields.get('full_name', '')} (id={record.get('id')})")
            return record
        except Exception as e:
            logger.error(f"Failed to create contact: {e}")
            return None

    def get_contacts(self, speaker_id: str) -> List[dict]:
        """Fetch all contact records for a speaker."""
        params = {'filterByFormula': f"{{speaker_id}} = '{speaker_id}'"}
        all_records = []
        offset = None
        while True:
            if offset:
                params['offset'] = offset
            try:
                resp = requests.get(
                    f'{self.base_url}/{self.contacts_table}',
                    headers=self.headers,
                    params=params,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                all_records.extend(data.get('records', []))
                offset = data.get('offset')
                if not offset:
                    break
            except Exception as e:
                logger.error(f"Failed to fetch contacts for {speaker_id}: {e}")
                break
        return all_records

    def get_contact_by_id(self, contact_id: str) -> Optional[dict]:
        """Fetch a single contact by Airtable record ID."""
        try:
            resp = requests.get(
                f'{self.base_url}/{self.contacts_table}/{contact_id}',
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch contact {contact_id}: {e}")
            return None

    def update_contact(self, contact_id: str, fields: dict) -> Optional[dict]:
        """Update a contact record."""
        payload = {'fields': clean_payload(fields)}
        try:
            resp = requests.patch(
                f'{self.base_url}/{self.contacts_table}/{contact_id}',
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to update contact {contact_id}: {e}")
            return None

    def delete_contact(self, contact_id: str) -> bool:
        """Delete a contact record."""
        try:
            resp = requests.delete(
                f'{self.base_url}/{self.contacts_table}/{contact_id}',
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to delete contact {contact_id}: {e}")
            return False
