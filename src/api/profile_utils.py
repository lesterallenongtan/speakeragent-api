"""Speaker profile JSON utilities — build, save, and trigger scout.

Single place to edit profile structure. Used by both dashboard_api and persona_api.
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PROFILE_DIR = Path('config/speaker_profiles')


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _parse_json_field(value) -> list:
    if not value:
        return []
    try:
        return json.loads(value) if isinstance(value, str) else list(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_topics(raw) -> list:
    """Normalize topics regardless of whether they came from Airtable (stored as JSON
    with title/abstract keys) or from a Pydantic body (already dicts with topic/description).
    """
    items = _parse_json_field(raw)
    result = []
    for t in items:
        if isinstance(t, dict):
            result.append({
                'topic': t.get('title', t.get('topic', '')),
                'description': t.get('abstract', t.get('description', '')),
                'audience': t.get('audience', ''),
            })
        else:
            result.append({'topic': str(t), 'description': '', 'audience': ''})
    return result


# ── Profile builders ───────────────────────────────────────────────────────────

def build_profile_from_fields(fields: dict) -> dict:
    """Build a profile dict from merged Airtable fields (Speakers + Speaker_Persona)."""
    topics = _parse_topics(fields.get('topics', ''))
    industries = _parse_json_field(fields.get('target_industries', ''))

    profile = {
        'full_name': fields.get('full_name', ''),
        'credentials': fields.get('credentials', ''),
        'professional_title': fields.get('tagline', ''),
        'years_experience': fields.get('years_experience', 0),
        'book_title': '',
        'topics': topics if topics else [{'topic': 'General', 'description': '', 'audience': ''}],
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
        'zip_code': fields.get('zip_code', ''),
    }
    if fields.get('bio'):
        profile['bio'] = fields['bio']
    return profile


def build_profile_from_body(body, full_name: str = '') -> dict:
    """Build a profile dict from a Pydantic registration or persona body.

    Works with both SpeakerRegistration (has body.full_name) and PersonaCreate
    (no full_name on body — pass it via the full_name kwarg instead).
    """
    topics = []
    discussion_points = []
    for t in (body.topics or []):
        topic_title = t.title if hasattr(t, 'title') else str(t)
        abstract = getattr(t, 'abstract', '') or ''
        audience = getattr(t, 'audience', '') or ''
        topics.append({'topic': topic_title, 'description': abstract, 'audience': audience})
        discussion_points.append(topic_title)
        phrase = topic_title.split(':')[0].strip()
        if phrase != topic_title:
            discussion_points.append(phrase)

    profile = {
        'full_name': getattr(body, 'full_name', None) or full_name,
        'credentials': getattr(body, 'credentials', None) or '',
        'professional_title': getattr(body, 'tagline', None) or '',
        'years_experience': getattr(body, 'years_experience', None) or 0,
        'book_title': '',
        'topics': topics if topics else [{'topic': 'General', 'description': '', 'audience': ''}],
        'target_industries': getattr(body, 'target_industries', None) or [],
        'target_geography': getattr(body, 'location', None) or 'National (US)',
        'min_honorarium': getattr(body, 'min_honorarium', None) or 0,
        'discussion_points': discussion_points[:10],
        'linkedin': getattr(body, 'linkedin', None) or '',
        'website': getattr(body, 'website', None) or '',
        'speaker_sheet': getattr(body, 'speaker_sheet', None) or '',
        'notes': getattr(body, 'notes', None) or '',
        'conference_year': getattr(body, 'conference_year', None) or date.today().year,
        'conference_tier': getattr(body, 'conference_tier', None) or '',
        'zip_code': getattr(body, 'zip_code', None) or '',
    }
    if getattr(body, 'bio', None):
        profile['bio'] = body.bio
    return profile


# ── Save / delete / run ────────────────────────────────────────────────────────

def delete_profile(speaker_id: str, persona_record_id: str = '') -> bool:
    """Delete the profile JSON file for a persona. Returns True if deleted."""
    filename = (
        f'{speaker_id}_{persona_record_id}.json' if persona_record_id
        else f'{speaker_id}.json'
    )
    path = PROFILE_DIR / filename
    if path.exists():
        path.unlink()
        logger.info(f'[PROFILE] Deleted profile file: {path}')
        return True
    logger.warning(f'[PROFILE] Profile file not found for deletion: {path}')
    return False


def save_profile(speaker_id: str, profile: dict, persona_record_id: str = '') -> str:
    """Persist profile dict to JSON. Returns the file path string."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    filename = (
        f'{speaker_id}_{persona_record_id}.json' if persona_record_id
        else f'{speaker_id}.json'
    )
    path = PROFILE_DIR / filename
    with open(path, 'w') as f:
        json.dump(profile, f, indent=2)
    return str(path)


def create_profile_and_run_scout(
    speaker_id: str,
    persona_record_id: str = '',
    *,
    body=None,
    fields: Optional[dict] = None,
    full_name: str = '',
    profile_cleaner: Optional[Callable[[dict], dict]] = None,
) -> None:
    """Build profile JSON from body or fields, save it, then trigger scout.

    Args:
        speaker_id:        Speaker identifier.
        persona_record_id: Airtable record ID of the persona row (used in filename).
        body:              Pydantic model (SpeakerRegistration or PersonaCreate).
        fields:            Flat dict from merged Airtable fields — alternative to body.
        full_name:         Speaker name when body has no full_name attribute.
        profile_cleaner:   Optional callable applied to the profile before saving
                           (e.g. _clean_profile_with_ai from dashboard_api).

    Lazy-imports _run_scout_for_speaker to avoid circular imports.
    """
    try:
        if body is not None:
            profile = build_profile_from_body(body, full_name=full_name)
        elif fields is not None:
            profile = build_profile_from_fields(fields)
        else:
            raise ValueError('Either body or fields must be provided')

        if profile_cleaner is not None:
            profile = profile_cleaner(profile)

        profile_path = save_profile(speaker_id, profile, persona_record_id)
        logger.info(f'[PROFILE] Saved → {profile_path}')

        # Lazy import to avoid circular dependency (dashboard_api imports profile_utils)
        from src.api.dashboard_api import _run_scout_for_speaker
        _run_scout_for_speaker(speaker_id, profile_path, persona_record_id)

        # Launch Apify podcast scraper concurrently — runs in parallel with scout results
        import threading as _threading
        from src.api.podcast_scraper import run_apify_podcast_scraper
        _podcast_thread = _threading.Thread(
            target=run_apify_podcast_scraper,
            args=(speaker_id, profile, persona_record_id),
            daemon=True,
            name=f'apify-podcast-{speaker_id}',
        )
        _podcast_thread.start()
        logger.info(
            f'[PROFILE] Apify podcast scraper thread launched for {speaker_id} '
            f'(thread={_podcast_thread.name})'
        )

    except Exception as e:
        logger.error(f'[PROFILE] Failed for {speaker_id}: {e}', exc_info=True)
