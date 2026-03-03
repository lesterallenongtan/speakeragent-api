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
    datetime_fields = {'Date Found', 'Event Date', 'created_at'}
    number_fields = {'Match Score', 'years_experience', 'min_honorarium'}

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
                 speakers_table: str = 'Speakers'):
        self.api_key = api_key
        self.base_id = base_id
        self.leads_table = leads_table
        self.speakers_table = speakers_table
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
                  status: str = '', triage: str = '') -> list:
        """Fetch leads with optional filters."""
        filters = []
        if speaker_id:
            filters.append(f"{{speaker_id}} = '{speaker_id}'")
        if status:
            filters.append(f"{{Lead Status}} = '{status}'")
        if triage:
            filters.append(f"{{Lead Triage}} = '{triage}'")

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

    def get_lead_stats(self, speaker_id: str) -> dict:
        """Get aggregated stats for a speaker's leads."""
        leads = self.get_leads(speaker_id=speaker_id)
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
            logger.error(f"Failed to update speaker {record_id}: {e}")
            return None

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
