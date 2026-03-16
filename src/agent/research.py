"""Research Agent — digs deeper into conference leads found by the Scout.

Takes a scraped conference page and enriches it with:
- Organizer details (who runs this event)
- Past speaker history (who have they booked before)
- Audience insights (who attends)
- Budget signals (do they pay speakers)
- Decision maker contact info

This runs AFTER the Scout finds a URL but BEFORE the Pitch agent
writes the hook — giving the Pitch agent richer context.
"""

import json
import logging
import re
from typing import Optional

import anthropic
import requests

logger = logging.getLogger(__name__)


def research_conference(
    scraped: dict,
    profile: dict,
    api_key: str,
    model: str = 'claude-haiku-4-5-20251001',
) -> dict:
    """Enrich a scraped conference with deeper research.

    Args:
        scraped: Output from scrape_page() in scraper.py
        profile: Speaker profile dict
        api_key: Claude API key
        model: Claude model to use (haiku is fast + cheap for research)

    Returns:
        Enriched dict with additional research fields merged in.
    """
    logger.info(f"[RESEARCH] Researching: {scraped.get('title', 'Unknown')}")

    # Run all research steps
    organizer_info  = _research_organizer(scraped, api_key, model)
    speaker_history = _research_speaker_history(scraped, api_key, model)
    audience_info   = _research_audience(scraped, profile, api_key, model)
    contact_info    = _extract_contact_info(scraped, api_key, model)
    budget_signals  = _analyze_budget_signals(scraped, api_key, model)

    # Merge all research into enriched dict
    enriched = {
        **scraped,
        'research': {
            'organizer':       organizer_info,
            'speaker_history': speaker_history,
            'audience':        audience_info,
            'contact':         contact_info,
            'budget':          budget_signals,
        }
    }

    # Promote key fields to top level for easy access by Pitch agent
    if contact_info.get('decision_maker_name'):
        enriched['contact_name']  = contact_info['decision_maker_name']
    if contact_info.get('decision_maker_title'):
        enriched['contact_title'] = contact_info['decision_maker_title']
    if contact_info.get('email'):
        enriched.setdefault('emails', [])
        if contact_info['email'] not in enriched['emails']:
            enriched['emails'].insert(0, contact_info['email'])
    if audience_info.get('audience_description'):
        enriched['audience_description'] = audience_info['audience_description']
    if speaker_history.get('typical_speaker_profile'):
        enriched['typical_speaker_profile'] = speaker_history['typical_speaker_profile']
    if budget_signals.get('estimated_honorarium'):
        enriched['estimated_honorarium'] = budget_signals['estimated_honorarium']

    logger.info(
        f"[RESEARCH] Complete for '{scraped.get('title', '')}': "
        f"organizer={bool(organizer_info.get('name'))}, "
        f"contact={bool(contact_info.get('email'))}, "
        f"history={bool(speaker_history.get('typical_speaker_profile'))}"
    )

    return enriched


def _research_organizer(scraped: dict, api_key: str, model: str) -> dict:
    """Extract organizer/host information from scraped content."""
    prompt = f"""Analyze this conference page and extract organizer information.

CONFERENCE: {scraped.get('title', '')}
URL: {scraped.get('url', '')}
CONTENT:
{scraped.get('full_text', '')[:1000]}

Extract and return ONLY valid JSON:
{{
  "name": "<organization name or empty string>",
  "type": "<association|hospital|corporate|university|nonprofit|media|unknown>",
  "established": "<year or empty string>",
  "size": "<large|medium|small|unknown>",
  "prestige": "<high|medium|low|unknown>"
}}"""

    return _call_claude_json(prompt, api_key, model, default={
        'name': '', 'type': 'unknown', 'established': '',
        'size': 'unknown', 'prestige': 'unknown'
    })


def _research_speaker_history(scraped: dict, api_key: str, model: str) -> dict:
    """Analyze what kind of speakers this conference typically books."""
    prompt = f"""Analyze this conference page to understand their speaker preferences.

CONFERENCE: {scraped.get('title', '')}
CONTENT:
{scraped.get('full_text', '')[:1200]}

Based on the content, infer their typical speaker profile and return ONLY valid JSON:
{{
  "typical_speaker_profile": "<description of speakers they typically book, or empty string>",
  "prefers_credentials": <true|false>,
  "prefers_authors": <true|false>,
  "prefers_practitioners": <true|false>,
  "past_speakers_mentioned": ["<name1>", "<name2>"],
  "speaker_diversity_focus": <true|false>
}}"""

    return _call_claude_json(prompt, api_key, model, default={
        'typical_speaker_profile': '',
        'prefers_credentials': False,
        'prefers_authors': False,
        'prefers_practitioners': True,
        'past_speakers_mentioned': [],
        'speaker_diversity_focus': False,
    })


def _research_audience(scraped: dict, profile: dict, api_key: str, model: str) -> dict:
    """Research who attends this conference and how they align with speaker."""
    prompt = f"""Analyze this conference to understand the audience and their alignment
with a speaker whose expertise is: {', '.join(t.get('title', '') for t in profile.get('topics', []))}.

CONFERENCE: {scraped.get('title', '')}
CONTENT:
{scraped.get('full_text', '')[:1000]}

Return ONLY valid JSON:
{{
  "audience_description": "<who attends this conference>",
  "estimated_size": "<number range or unknown>",
  "professional_level": "<executive|manager|practitioner|mixed|unknown>",
  "industries": ["<industry1>", "<industry2>"],
  "alignment_with_speaker": "<high|medium|low>",
  "alignment_reason": "<one sentence why>"
}}"""

    return _call_claude_json(prompt, api_key, model, default={
        'audience_description': '',
        'estimated_size': 'unknown',
        'professional_level': 'unknown',
        'industries': [],
        'alignment_with_speaker': 'medium',
        'alignment_reason': '',
    })


def _extract_contact_info(scraped: dict, api_key: str, model: str) -> dict:
    """Extract the best contact person for speaker inquiries."""
    # First try to find emails from already-scraped content
    emails = scraped.get('emails', [])
    text   = scraped.get('full_text', '')

    # Find additional emails via regex
    found_emails = re.findall(
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
        text
    )
    all_emails = list(dict.fromkeys(emails + found_emails))  # dedupe preserving order

    prompt = f"""From this conference page, identify the best person to contact about speaking opportunities.

CONFERENCE: {scraped.get('title', '')}
EMAILS FOUND: {all_emails[:5]}
CONTENT:
{text[:1000]}

Return ONLY valid JSON:
{{
  "decision_maker_name": "<full name or empty string>",
  "decision_maker_title": "<job title or empty string>",
  "email": "<best email for speaker inquiries or empty string>",
  "linkedin": "<linkedin url or empty string>",
  "contact_method": "<email|form|linkedin|website|unknown>"
}}"""

    result = _call_claude_json(prompt, api_key, model, default={
        'decision_maker_name': '',
        'decision_maker_title': '',
        'email': all_emails[0] if all_emails else '',
        'linkedin': '',
        'contact_method': 'unknown',
    })

    # Use scraped email as fallback
    if not result.get('email') and all_emails:
        result['email'] = all_emails[0]

    return result


def _analyze_budget_signals(scraped: dict, api_key: str, model: str) -> dict:
    """Analyze budget signals to estimate if/how much they pay speakers."""
    prompt = f"""Analyze this conference page for speaker compensation signals.

CONFERENCE: {scraped.get('title', '')}
MENTIONS PAYMENT: {scraped.get('mentions_payment', False)}
MENTIONS NO PAYMENT: {scraped.get('mentions_no_payment', False)}
CONTENT:
{scraped.get('full_text', '')[:800]}

Return ONLY valid JSON:
{{
  "pays_speakers": <true|false|null>,
  "estimated_honorarium": "<range or unknown>",
  "covers_travel": <true|false|null>,
  "covers_hotel": <true|false|null>,
  "budget_confidence": "<high|medium|low>",
  "budget_notes": "<any specific mentions of compensation>"
}}"""

    return _call_claude_json(prompt, api_key, model, default={
        'pays_speakers': None,
        'estimated_honorarium': 'unknown',
        'covers_travel': None,
        'covers_hotel': None,
        'budget_confidence': 'low',
        'budget_notes': '',
    })


def _call_claude_json(
    prompt: str,
    api_key: str,
    model: str,
    default: dict,
) -> dict:
    """Call Claude and parse JSON response. Returns default on any failure."""
    try:
        client   = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1]
            raw = raw.rsplit('```', 1)[0]

        return json.loads(raw)

    except json.JSONDecodeError as e:
        logger.warning(f"[RESEARCH] JSON parse failed: {e}")
        return default
    except Exception as e:
        logger.warning(f"[RESEARCH] Claude call failed: {e}")
        return default
