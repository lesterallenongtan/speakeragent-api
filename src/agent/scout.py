"""Scout orchestrator — the main pipeline.

Ties together search → scrape → score → pitch → push.
"""

import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from config.settings import Settings
from src.agent.scraper import generate_search_queries, web_search, scrape_page
from src.agent.scoring import score_lead_with_claude, classify_triage
from src.agent.pitch import generate_hook
from src.api.airtable import AirtableAPI

logger = logging.getLogger(__name__)


def _log(msg: str):
    """Print to stderr for guaranteed visibility in Railway logs."""
    print(msg, file=sys.stderr, flush=True)


def load_profile(profile_path: str) -> dict:
    """Load a speaker profile from JSON file."""
    path = Path(profile_path)
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")
    with open(path) as f:
        return json.load(f)


def run_scout(
    profile_path: str,
    speaker_id: str = 'leigh_vinocur',
    max_leads: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Run the full scouting pipeline.

    Args:
        profile_path: Path to speaker profile JSON.
        speaker_id: Unique ID for this speaker (for Airtable filtering).
        max_leads: Max leads to process (None = use settings).
        dry_run: If True, skip Airtable push (print results instead).

    Returns:
        Summary dict with counts and results.
    """
    # Setup
    settings = Settings()
    profile = load_profile(profile_path)
    if max_leads is None:
        max_leads = settings.MAX_LEADS_PER_RUN

    airtable = AirtableAPI(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
        leads_table=settings.LEADS_TABLE,
        speakers_table=settings.SPEAKERS_TABLE,
    )

    # Verify Airtable connection
    if not dry_run:
        if not airtable.health_check():
            logger.error("Airtable connection failed. Check your API key and Base ID.")
            return {'error': 'Airtable connection failed'}

    summary = {
        'total_urls': 0,
        'scraped': 0,
        'scored': 0,
        'pushed': 0,
        'skipped_duplicate': 0,
        'skipped_scrape_fail': 0,
        'skipped_score_fail': 0,
        'triage_counts': {'RED': 0, 'YELLOW': 0, 'GREEN': 0},
        'leads': [],
    }

    # Step 1: Generate search queries
    queries = generate_search_queries(profile)
    _log(f"[SCOUT] Generated {len(queries)} search queries")
    for i, q in enumerate(queries):
        _log(f"  Q{i+1}: {q}")

    # Step 2: Search for conference URLs
    seed_path = str(Path(profile_path).parent.parent / 'seed_urls.json')
    _log(f"[SCOUT] Seed URL path: {seed_path} (exists={Path(seed_path).exists()})")
    urls = web_search(queries, results_per_query=3, delay=2.0, seed_urls_path=seed_path)
    summary['total_urls'] = len(urls)
    _log(f"[SCOUT] Found {len(urls)} unique URLs to process")

    if not urls:
        _log("[SCOUT] WARNING: No URLs found from any source!")
        return summary

    # Step 3-5: Process each URL
    processed = 0
    for i, url in enumerate(urls):
        if processed >= max_leads:
            _log(f"[SCOUT] Reached max leads ({max_leads}), stopping.")
            break

        _log(f"[SCOUT] [{i+1}/{len(urls)}] Processing: {url}")

        # Step 3a: Scrape
        scraped = scrape_page(url)
        if not scraped:
            summary['skipped_scrape_fail'] += 1
            _log(f"[SCOUT] [{i+1}] SKIP: Scrape failed for {url}")
            continue
        summary['scraped'] += 1

        conf_name = scraped.get('title', url)[:200]
        if not conf_name or conf_name == url:
            conf_name = url.split('/')[2]  # Use domain as fallback

        _log(f"[SCOUT] [{i+1}] Title: {conf_name}")

        # Step 3b: Check for duplicates
        if not dry_run and airtable.lead_exists(speaker_id, conf_name):
            summary['skipped_duplicate'] += 1
            _log(f"[SCOUT] [{i+1}] SKIP: Duplicate")
            continue

        # Step 3c: Score with Claude
        score_result = score_lead_with_claude(
            scraped=scraped,
            profile=profile,
            api_key=settings.CLAUDE_API_KEY,
            model=settings.CLAUDE_MODEL,
        )
        if not score_result:
            summary['skipped_score_fail'] += 1
            _log(f"[SCOUT] [{i+1}] SKIP: Scoring failed")
            continue
        summary['scored'] += 1

        match_score = score_result['match_score']
        triage = score_result['triage']
        best_topic = score_result['best_topic']
        summary['triage_counts'][triage] += 1

        _log(f"[SCOUT] [{i+1}] Score: {match_score}/100 → {triage} | Topic: {best_topic}")

        # Step 3d: Generate hook (skip for RED — poor match)
        hook = ''
        cta = ''
        if match_score >= 35:
            pitch_result = generate_hook(
                profile=profile,
                scraped=scraped,
                best_topic=best_topic,
                api_key=settings.CLAUDE_API_KEY,
                model=settings.CLAUDE_MODEL,
            )
            hook = pitch_result.get('hook', '')
            cta = pitch_result.get('cta', '')
            _log(f"[SCOUT] [{i+1}] Hook generated ({len(hook)} chars)")
        else:
            _log(f"[SCOUT] [{i+1}] Hook SKIPPED (RED lead, score < 35)")

        # Step 3e: Build Airtable payload
        lead_payload = {
            'Conference Name': conf_name,
            'Date Found': date.today().isoformat(),
            'Lead Triage': triage,
            'Match Score': match_score,
            'Pay Estimate': score_result.get('pay_estimate', ''),
            'Conference URL': url if url.startswith('http') else f'https://{url}',
            'Suggested Talk': best_topic,
            'The Hook': hook,
            'CTA': cta,
            'Lead Status': 'New',
            'speaker_id': speaker_id,
        }

        # Add optional fields only if present
        if scraped.get('location'):
            lead_payload['Event Location'] = scraped['location']
        if scraped.get('emails'):
            lead_payload['Contact Email'] = scraped['emails'][0]
        if scraped.get('linkedin_links'):
            lead_payload['Contact LinkedIn'] = scraped['linkedin_links'][0]

        # Parse event date if possible
        event_date_iso = _parse_date_to_iso(scraped.get('event_date_raw', ''))
        if event_date_iso:
            lead_payload['Event Date'] = event_date_iso

        # Step 3f: Push to Airtable
        if dry_run:
            _log(f"[SCOUT] [{i+1}] DRY RUN — would push: {conf_name}")
            summary['pushed'] += 1
        else:
            result = airtable.push_lead(lead_payload)
            if result:
                summary['pushed'] += 1
                _log(f"[SCOUT] [{i+1}] PUSHED to Airtable: {conf_name}")
            else:
                _log(f"[SCOUT] [{i+1}] PUSH FAILED (may be duplicate): {conf_name}")

        summary['leads'].append({
            'conference': conf_name,
            'score': match_score,
            'triage': triage,
            'topic': best_topic,
            'url': url,
        })
        processed += 1

    # Print summary
    _log(f"[SCOUT] ====== RUN COMPLETE ======")
    _log(f"[SCOUT]   URLs found:        {summary['total_urls']}")
    _log(f"[SCOUT]   Successfully scraped: {summary['scraped']}")
    _log(f"[SCOUT]   Scored:            {summary['scored']}")
    _log(f"[SCOUT]   Pushed to Airtable: {summary['pushed']}")
    _log(f"[SCOUT]   Skipped (duplicate): {summary['skipped_duplicate']}")
    _log(f"[SCOUT]   Skipped (scrape fail): {summary['skipped_scrape_fail']}")
    _log(f"[SCOUT]   Skipped (score fail): {summary['skipped_score_fail']}")
    _log(f"[SCOUT]   Triage: GREEN={summary['triage_counts']['GREEN']} "
         f"YELLOW={summary['triage_counts']['YELLOW']} "
         f"RED={summary['triage_counts']['RED']}")

    return summary


def _parse_date_to_iso(date_str: str) -> Optional[str]:
    """Try to parse a date string into YYYY-MM-DD format."""
    if not date_str:
        return None
    import re
    from datetime import datetime

    # Try common formats
    date_str = date_str.strip()
    # Handle ranges like "March 15-17, 2026" → take first date
    date_str = re.sub(r'(\d{1,2})\s*[-–]\s*\d{1,2}', r'\1', date_str)

    formats = [
        '%B %d, %Y',       # March 15, 2026
        '%B %d %Y',        # March 15 2026
        '%b %d, %Y',       # Mar 15, 2026
        '%b %d %Y',        # Mar 15 2026
        '%m/%d/%Y',        # 03/15/2026
        '%Y-%m-%d',        # 2026-03-15
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None
