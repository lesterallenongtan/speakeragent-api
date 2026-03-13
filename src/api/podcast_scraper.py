"""Apify podcast directory scraper integration.

Starts a ryanclinton~podcast-directory-scraper actor run, polls for results
(up to 30 minutes), and saves podcast leads to Airtable.

Entry point: run_apify_podcast_scraper() — designed to run inside a daemon thread.
"""

import logging
import time
from datetime import date
from typing import Optional

import requests

from config.settings import Settings
from src.api.airtable import AirtableAPI
from src.agent.scraper import generate_search_queries

logger = logging.getLogger(__name__)

APIFY_BASE_URL = 'https://api.apify.com/v2'
ACTOR_ID = 'ryanclinton~podcast-directory-scraper'
DEFAULT_POLL_INTERVAL = 60   # seconds between each status poll
DEFAULT_TIMEOUT = 30 * 60    # 30 minutes in seconds


# ---------------------------------------------------------------------------
# Query extraction
# ---------------------------------------------------------------------------

def extract_podcast_queries(profile: dict) -> list:
    """Return query strings for all Podcast-typed queries generated from profile.

    Filters generate_search_queries() output to event_type == 'Podcast'.
    Returns a deduplicated list of query strings.
    """
    logger.info(
        f"[APIFY] Generating search queries for speaker='{profile.get('full_name', 'unknown')}'"
    )
    all_queries = generate_search_queries(profile)
    podcast_queries = [q for q, t in all_queries if t == 'Podcast']

    logger.info(
        f"[APIFY] Extracted {len(podcast_queries)} podcast queries "
        f"(from {len(all_queries)} total queries)"
    )
    for i, q in enumerate(podcast_queries, 1):
        logger.debug(f"[APIFY] Podcast query {i}/{len(podcast_queries)}: {q}")

    return podcast_queries


# ---------------------------------------------------------------------------
# Start Apify run
# ---------------------------------------------------------------------------

def _start_apify_run(keywords: list, token: str) -> Optional[str]:
    """POST to Apify to start a podcast-directory-scraper actor run.

    Returns the run ID string on success, None on failure.
    """
    url = f'{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs'
    params = {'token': token}
    payload = {
        'activeOnly': False,
        'includeEpisodes': False,
        'maxResults': 100,
        'proxyConfiguration': {
            'useApifyProxy': True,
            'apifyProxyGroups': ['RESIDENTIAL'],
            'apifyProxyCountry': 'US',
        },
        'searchTerms': keywords,
    }

    preview = keywords[:5]
    logger.info(
        f"[APIFY] Starting actor run — {len(keywords)} keywords. "
        f"First 5: {preview}{'...' if len(keywords) > 5 else ''}"
    )
    logger.debug(f"[APIFY] POST {url} payload keywords count={len(keywords)}")

    try:
        resp = requests.post(url, params=params, json=payload, timeout=30)
        logger.info(f"[APIFY] Start run response — HTTP {resp.status_code}")

        if resp.status_code not in (200, 201):
            logger.error(
                f"[APIFY] Failed to start run — HTTP {resp.status_code}: "
                f"{resp.text[:400]}"
            )
            return None

        data = resp.json()
        run_id = (data.get('data') or {}).get('id', '')
        if not run_id:
            logger.error(
                f"[APIFY] Start run response missing run ID. Full response: {data}"
            )
            return None

        logger.info(f"[APIFY] Actor run started successfully — run_id={run_id}")
        return run_id

    except requests.exceptions.Timeout:
        logger.error("[APIFY] Timeout (30s) while starting actor run")
        return None
    except Exception as e:
        logger.error(f"[APIFY] Unexpected error starting actor run: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Poll for results
# ---------------------------------------------------------------------------

def _poll_for_results(
    run_id: str,
    token: str,
    timeout_sec: int = DEFAULT_TIMEOUT,
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL,
) -> Optional[list]:
    """Poll Apify for run results until SUCCEEDED or timeout.

    Uses the run-specific dataset endpoint (not /runs/last/) to avoid
    collisions when multiple speakers register concurrently.

    Polls every poll_interval_sec seconds.
    Returns list of dataset items on success, None on timeout or fatal error.
    """
    status_url = f'{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs/{run_id}'
    items_url = f'{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs/{run_id}/dataset/items'
    params = {'token': token}

    start_time = time.monotonic()
    deadline = start_time + timeout_sec
    attempt = 0

    logger.info(
        f"[APIFY] Starting poll loop — run_id={run_id} "
        f"timeout={timeout_sec}s interval={poll_interval_sec}s"
    )

    while time.monotonic() < deadline:
        attempt += 1
        elapsed = int(time.monotonic() - start_time)
        remaining = int(deadline - time.monotonic())

        # --- Check run status ---
        try:
            status_resp = requests.get(status_url, params=params, timeout=15)
            if status_resp.status_code == 200:
                run_data = (status_resp.json().get('data') or {})
                run_status = run_data.get('status', 'UNKNOWN')
                logger.info(
                    f"[APIFY] Poll attempt {attempt}: run_id={run_id} "
                    f"status={run_status} elapsed={elapsed}s remaining={remaining}s"
                )
                if run_status == 'FAILED':
                    logger.error(
                        f"[APIFY] Run {run_id} reported FAILED — aborting poll"
                    )
                    return None
                if run_status == 'ABORTED':
                    logger.warning(
                        f"[APIFY] Run {run_id} was ABORTED — aborting poll"
                    )
                    return None
            else:
                logger.warning(
                    f"[APIFY] Status check HTTP {status_resp.status_code} "
                    f"for run_id={run_id} (attempt {attempt})"
                )
        except requests.exceptions.Timeout:
            logger.warning(
                f"[APIFY] Status check timed out on attempt {attempt} "
                f"for run_id={run_id}"
            )
        except Exception as e:
            logger.warning(
                f"[APIFY] Status check error on attempt {attempt}: {e}"
            )

        # --- Fetch dataset items (only populated once SUCCEEDED) ---
        try:
            items_resp = requests.get(items_url, params=params, timeout=30)
            logger.debug(
                f"[APIFY] Dataset fetch attempt {attempt} — "
                f"HTTP {items_resp.status_code}"
            )
            if items_resp.status_code == 200:
                items = items_resp.json()
                if isinstance(items, list) and len(items) > 0:
                    logger.info(
                        f"[APIFY] SUCCESS — received {len(items)} items "
                        f"on attempt {attempt} after {elapsed}s"
                    )
                    return items
                else:
                    logger.info(
                        f"[APIFY] Dataset empty on attempt {attempt} — "
                        f"run not yet SUCCEEDED or returned 0 results"
                    )
            else:
                logger.warning(
                    f"[APIFY] Dataset fetch HTTP {items_resp.status_code} "
                    f"on attempt {attempt}: {items_resp.text[:200]}"
                )
        except requests.exceptions.Timeout:
            logger.warning(
                f"[APIFY] Dataset fetch timed out on attempt {attempt}"
            )
        except Exception as e:
            logger.warning(
                f"[APIFY] Dataset fetch error on attempt {attempt}: {e}"
            )

        # --- Sleep before next poll ---
        if time.monotonic() < deadline:
            logger.debug(
                f"[APIFY] Sleeping {poll_interval_sec}s before next poll "
                f"(attempt {attempt} done)"
            )
            time.sleep(poll_interval_sec)

    logger.error(
        f"[APIFY] TIMEOUT — no SUCCEEDED results after {timeout_sec}s "
        f"({attempt} poll attempts) for run_id={run_id}"
    )
    return None


# ---------------------------------------------------------------------------
# Process and save leads
# ---------------------------------------------------------------------------

def _process_and_save_leads(
    items: list,
    speaker_id: str,
    persona_record_id: str,
    profile: dict,
) -> dict:
    """Map Apify podcast items to Airtable lead fields and push each one.

    Returns summary dict: {total, pushed, skipped_duplicate, failed}.
    """
    settings = Settings()
    at = AirtableAPI(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
        leads_table=settings.LEADS_TABLE,
        speakers_table=settings.SPEAKERS_TABLE,
    )

    summary = {
        'total': len(items),
        'pushed': 0,
        'skipped_duplicate': 0,
        'failed': 0,
    }

    logger.info(
        f"[APIFY] Processing {len(items)} podcast items for speaker={speaker_id}"
    )

    for idx, item in enumerate(items, 1):
        # Resolve podcast name from multiple possible field names
        podcast_name = (
            item.get('title') or
            item.get('name') or
            item.get('podcastName') or
            item.get('podcast_name') or
            ''
        )[:200].strip()

        # Resolve URL from multiple possible field names
        podcast_url = (
            item.get('url') or
            item.get('websiteUrl') or
            item.get('website_url') or
            item.get('link') or
            item.get('rssUrl') or
            item.get('rss_url') or
            ''
        ).strip()

        if not podcast_name:
            logger.warning(
                f"[APIFY] [{idx}/{len(items)}] Item has no usable title — skipping. "
                f"Raw keys: {list(item.keys())}"
            )
            summary['failed'] += 1
            continue

        logger.info(
            f"[APIFY] [{idx}/{len(items)}] Processing: '{podcast_name}' "
            f"url={podcast_url or '(none)'}"
        )

        # Normalise URL scheme
        if podcast_url and not podcast_url.startswith('http'):
            podcast_url = f'https://{podcast_url}'

        # Build Airtable lead payload matching existing Conferences table schema
        lead_payload = {
            'Conference Name': podcast_name,
            'Conference URL': podcast_url,
            'Type': 'Podcast',
            'Lead Status': 'New',
            'Lead Triage': 'YELLOW',
            'Match Score': 50,        # neutral — not Claude-scored; directory-matched
            'Date Found': date.today().isoformat(),
            'speaker_id': speaker_id,
        }

        # Optional enrichment
        description = (
            item.get('description') or
            item.get('summary') or
            item.get('about') or
            ''
        ).strip()
        if description:
            lead_payload['Suggested Talk'] = description[:500]

        contact_email = (
            item.get('email') or
            item.get('contactEmail') or
            item.get('contact_email') or
            ''
        ).strip()
        if contact_email:
            lead_payload['Contact Email'] = contact_email

        # RSS URL as fallback Conference URL if websiteUrl is absent
        rss_url = (item.get('rssUrl') or item.get('rss_url') or '').strip()
        if rss_url and not podcast_url:
            if not rss_url.startswith('http'):
                rss_url = f'https://{rss_url}'
            lead_payload['Conference URL'] = rss_url

        if persona_record_id:
            lead_payload['persona_id'] = persona_record_id

        # Push to Airtable (push_lead handles dedup check internally)
        result = at.push_lead(lead_payload)
        if result is None:
            # push_lead returns None for both duplicates and genuine push failures
            summary['skipped_duplicate'] += 1
            logger.info(
                f"[APIFY] [{idx}/{len(items)}] Skipped (duplicate or push failed): "
                f"'{podcast_name}'"
            )
        else:
            summary['pushed'] += 1
            logger.info(
                f"[APIFY] [{idx}/{len(items)}] Pushed to Airtable: '{podcast_name}' "
                f"record_id={result.get('id')}"
            )

    logger.info(
        f"[APIFY] Save complete for speaker={speaker_id}: "
        f"total={summary['total']} pushed={summary['pushed']} "
        f"skipped={summary['skipped_duplicate']} failed={summary['failed']}"
    )
    return summary


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_apify_podcast_scraper(
    speaker_id: str,
    profile: dict,
    persona_record_id: str = '',
) -> None:
    """Orchestrate the full Apify podcast scraper pipeline for one speaker.

    Designed to run inside a daemon thread. Logs all failures and exits
    cleanly on timeout, missing token, or API errors.
    """
    try:
        settings = Settings()
        token = settings.APIFY_TOKEN

        if not token:
            logger.warning(
                f"[APIFY] APIFY_TOKEN not configured — skipping podcast scraper "
                f"for speaker={speaker_id}"
            )
            return

        logger.info(
            f"[APIFY] ===== Podcast scraper STARTING "
            f"speaker={speaker_id} persona={persona_record_id or '(none)'} ====="
        )

        # Step 1: Extract podcast-type query strings from profile
        keywords = extract_podcast_queries(profile)
        if not keywords:
            logger.warning(
                f"[APIFY] No podcast queries generated for speaker={speaker_id} — "
                f"check profile topics/industries. Aborting."
            )
            return

        # Step 2: Start the Apify actor run
        run_id = _start_apify_run(keywords, token)
        if not run_id:
            logger.error(
                f"[APIFY] Failed to start actor run for speaker={speaker_id} — aborting"
            )
            return

        # Step 3: Poll for results with 30-minute timeout
        logger.info(
            f"[APIFY] Polling for results — run_id={run_id} "
            f"timeout={DEFAULT_TIMEOUT}s interval={DEFAULT_POLL_INTERVAL}s"
        )
        items = _poll_for_results(
            run_id=run_id,
            token=token,
            timeout_sec=DEFAULT_TIMEOUT,
            poll_interval_sec=DEFAULT_POLL_INTERVAL,
        )

        if items is None:
            logger.error(
                f"[APIFY] No results received within {DEFAULT_TIMEOUT}s timeout "
                f"for speaker={speaker_id} run_id={run_id} — terminating scout run"
            )
            return

        if len(items) == 0:
            logger.warning(
                f"[APIFY] Actor returned 0 items for speaker={speaker_id} "
                f"run_id={run_id} — nothing to save"
            )
            return

        logger.info(
            f"[APIFY] Received {len(items)} podcast items — beginning Airtable save "
            f"for speaker={speaker_id}"
        )

        # Step 4: Process and save to Airtable
        summary = _process_and_save_leads(items, speaker_id, persona_record_id, profile)

        logger.info(
            f"[APIFY] ===== Podcast scraper COMPLETE "
            f"speaker={speaker_id} "
            f"pushed={summary['pushed']} "
            f"skipped={summary['skipped_duplicate']} "
            f"failed={summary['failed']} ====="
        )

    except Exception as e:
        logger.error(
            f"[APIFY] Unhandled exception in podcast scraper for speaker={speaker_id}: {e}",
            exc_info=True,
        )
