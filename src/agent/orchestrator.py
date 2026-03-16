"""Orchestrator — coordinates the full multi-agent pipeline.

Pipeline:
    Scout Agent     → finds conference URLs
    Research Agent  → enriches each URL with deeper context
    Pitch Agent     → generates personalized hook + CTA
    Orchestrator    → manages the whole flow, handles errors,
                      pushes results to Airtable

This is the entry point for the multi-agent pipeline.
Call run_pipeline() to execute the full flow.
"""

import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional

from config.settings import Settings
from src.agent.scraper import generate_search_queries, web_search, scrape_page
from src.agent.scoring import score_lead_with_claude, classify_triage
from src.agent.pitch import generate_hook
from src.agent.verifier import verify_lead
from src.agent.research import research_conference
from src.api.airtable import AirtableAPI

logger = logging.getLogger(__name__)


def run_pipeline(
    profile_path: str,
    speaker_id: str = 'leigh_vinocur',
    max_leads: Optional[int] = None,
    dry_run: bool = False,
    persona_record_id: str = '',
    enable_research: bool = True,
) -> dict:
    """Run the full multi-agent pipeline.

    Agents run in this order for each URL:
        1. Scout   — find URLs (parallel across all URLs)
        2. Research — enrich each URL with deeper context
        3. Score   — score the enriched lead
        4. Verify  — verify the lead is valid
        5. Pitch   — generate personalized hook + CTA
        6. Push    — save to Airtable

    Args:
        profile_path:      Path to speaker profile JSON
        speaker_id:        Unique speaker ID for Airtable filtering
        max_leads:         Max leads to push (None = use settings)
        dry_run:           If True skip Airtable push
        persona_record_id: Airtable persona record ID to link leads to
        enable_research:   If False skip Research agent (faster, less thorough)

    Returns:
        Summary dict with counts and per-lead results.
    """
    settings = Settings()
    profile  = _load_profile(profile_path)
    if max_leads is None:
        max_leads = settings.MAX_LEADS_PER_RUN

    airtable = AirtableAPI(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
        leads_table=settings.LEADS_TABLE,
        speakers_table=settings.SPEAKERS_TABLE,
    )

    if not dry_run and not airtable.health_check():
        logger.error("[ORCHESTRATOR] Airtable connection failed.")
        return {'error': 'Airtable connection failed'}

    summary = {
        'total_urls': 0,
        'scraped': 0,
        'researched': 0,
        'scored': 0,
        'pushed': 0,
        'skipped_duplicate': 0,
        'skipped_scrape_fail': 0,
        'skipped_score_fail': 0,
        'skipped_rejected': 0,
        'triage_counts': {'RED': 0, 'YELLOW': 0, 'GREEN': 0},
        'leads': [],
        'agent_runs': {
            'scout': 0,
            'research': 0,
            'pitch': 0,
        }
    }

    # ── AGENT 1: SCOUT ───────────────────────────────────────────────────────
    logger.info("[ORCHESTRATOR] Starting Scout Agent...")
    url_type_map = _run_scout_agent(profile, profile_path, settings)
    summary['total_urls']         = len(url_type_map)
    summary['agent_runs']['scout'] = len(url_type_map)
    logger.info(f"[ORCHESTRATOR] Scout Agent found {len(url_type_map)} URLs")

    if not url_type_map:
        logger.warning("[ORCHESTRATOR] No URLs found. Exiting pipeline.")
        return summary

    # ── AGENTS 2-5: RESEARCH → SCORE → VERIFY → PITCH (per URL) ─────────────
    lock      = threading.Lock()
    processed = 0
    url_items = list(url_type_map.items())
    total     = len(url_items)

    def _process_url(args: tuple) -> Optional[dict]:
        """Run Research → Score → Verify → Pitch for a single URL."""
        idx, url, event_type = args

        _at = AirtableAPI(
            api_key=settings.AIRTABLE_API_KEY,
            base_id=settings.AIRTABLE_BASE_ID,
            leads_table=settings.LEADS_TABLE,
            speakers_table=settings.SPEAKERS_TABLE,
        )

        result = {
            'scraped': 0, 'researched': 0, 'scored': 0, 'pushed': 0,
            'skipped_scrape_fail': 0, 'skipped_duplicate': 0,
            'skipped_score_fail': 0, 'skipped_rejected': 0,
            'triage': None, 'lead': None,
            'agent_research': 0, 'agent_pitch': 0,
        }

        logger.info(f"[ORCHESTRATOR] [{idx}/{total}] Processing: {url} [{event_type}]")

        # ── Step 1: Scrape ────────────────────────────────────────────────
        scraped = scrape_page(url)
        if not scraped:
            result['skipped_scrape_fail'] = 1
            logger.info(f"[ORCHESTRATOR] [{idx}] SKIP: Scrape failed")
            return result
        result['scraped'] = 1

        conf_name = scraped.get('title', url)[:200]
        logger.info(f"[ORCHESTRATOR] [{idx}] Scraped: {conf_name}")

        # ── Step 2: Dedup check ───────────────────────────────────────────
        if not dry_run and _at.lead_exists(speaker_id, conf_name):
            result['skipped_duplicate'] = 1
            logger.info(f"[ORCHESTRATOR] [{idx}] SKIP: Duplicate")
            return result

        # ── AGENT 2: RESEARCH ─────────────────────────────────────────────
        enriched = scraped
        if enable_research and settings.CLAUDE_API_KEY:
            logger.info(f"[ORCHESTRATOR] [{idx}] Research Agent running...")
            enriched = research_conference(
                scraped=scraped,
                profile=profile,
                api_key=settings.CLAUDE_API_KEY,
                model='claude-haiku-4-5-20251001',  # Use Haiku — fast + cheap
            )
            result['researched']      = 1
            result['agent_research']  = 1
            logger.info(
                f"[ORCHESTRATOR] [{idx}] Research complete — "
                f"contact: {bool(enriched.get('contact_name'))}, "
                f"audience: {bool(enriched.get('audience_description'))}"
            )
        else:
            logger.info(f"[ORCHESTRATOR] [{idx}] Research Agent skipped (no API key or disabled)")

        # ── AGENT 3: SCORE ────────────────────────────────────────────────
        score_result = score_lead_with_claude(
            scraped=enriched,
            profile=profile,
            api_key=settings.CLAUDE_API_KEY,
            model=settings.CLAUDE_MODEL,
            event_type=event_type,
        )
        if not score_result:
            result['skipped_score_fail'] = 1
            logger.info(f"[ORCHESTRATOR] [{idx}] SKIP: Scoring failed")
            return result
        result['scored'] = 1

        match_score = score_result['match_score']
        triage      = score_result['triage']
        best_topic  = score_result['best_topic']
        result['triage'] = triage
        logger.info(f"[ORCHESTRATOR] [{idx}] Score: {match_score}/100 → {triage} | {best_topic}")

        # ── Verify ────────────────────────────────────────────────────────
        verification = verify_lead(
            lead_data={
                'Conference Name': conf_name,
                'Match Score': match_score,
                'Event Location': enriched.get('location', ''),
            },
            scraped=enriched,
            profile=profile,
            api_key=settings.CLAUDE_API_KEY,
            event_type=event_type,
        )
        logger.info(
            f"[ORCHESTRATOR] [{idx}] Verification: "
            f"{verification['status']} — {verification.get('notes', '')}"
        )

        if verification['status'] == 'Rejected':
            result['skipped_rejected'] = 1
            logger.info(f"[ORCHESTRATOR] [{idx}] SKIP: Rejected by verifier")
            return result

        # ── AGENT 4: PITCH ────────────────────────────────────────────────
        hook = ''
        cta  = ''
        if match_score >= 35:
            logger.info(f"[ORCHESTRATOR] [{idx}] Pitch Agent running...")
            pitch_result = generate_hook(
                profile=profile,
                scraped=enriched,       # Pass enriched (not just scraped)
                best_topic=best_topic,
                api_key=settings.CLAUDE_API_KEY,
                model=settings.CLAUDE_MODEL,
            )
            hook = pitch_result.get('hook', '')
            cta  = pitch_result.get('cta', '')
            result['agent_pitch'] = 1
            logger.info(f"[ORCHESTRATOR] [{idx}] Pitch Agent complete ({len(hook)} chars)")
        else:
            logger.info(f"[ORCHESTRATOR] [{idx}] Pitch Agent skipped (score < 35)")

        # ── Build lead payload ────────────────────────────────────────────
        research = enriched.get('research', {})
        lead_payload = {
            'Conference Name':      conf_name,
            'Date Found':           date.today().isoformat(),
            'Lead Triage':          triage,
            'Match Score':          match_score,
            'Pay Estimate':         score_result.get('pay_estimate', ''),
            'Conference URL':       url if url.startswith('http') else f'https://{url}',
            'Suggested Talk':       best_topic,
            'The Hook':             hook,
            'CTA':                  cta,
            'Lead Status':          'New',
            'speaker_id':           speaker_id,
            'Verification Status':  verification['status'],
            'Verification Notes':   verification.get('notes', ''),
            'Type':                 event_type,
        }

        # Add research-enriched fields if available
        if enriched.get('location'):
            lead_payload['Event Location'] = enriched['location']
        if enriched.get('contact_name'):
            lead_payload['Contact Name'] = enriched['contact_name']
        if enriched.get('contact_title'):
            lead_payload['Contact Title'] = enriched['contact_title']
        if enriched.get('emails'):
            lead_payload['Contact Email'] = enriched['emails'][0]
        if enriched.get('linkedin_links'):
            lead_payload['Contact LinkedIn'] = enriched['linkedin_links'][0]
        if persona_record_id:
            lead_payload['persona_id'] = persona_record_id

        # Parse event date
        event_date_iso = _parse_date_to_iso(enriched.get('event_date_raw', ''))
        if event_date_iso:
            lead_payload['Event Date'] = event_date_iso

        # ── Push to Airtable ──────────────────────────────────────────────
        if dry_run:
            logger.info(f"[ORCHESTRATOR] [{idx}] DRY RUN — would push: {conf_name}")
            result['pushed'] = 1
        else:
            push_result = _at.push_lead(lead_payload)
            if push_result:
                result['pushed'] = 1
                logger.info(f"[ORCHESTRATOR] [{idx}] PUSHED: {conf_name}")
            else:
                logger.info(f"[ORCHESTRATOR] [{idx}] PUSH FAILED (duplicate?): {conf_name}")

        result['lead'] = {
            'conference': conf_name,
            'score':      match_score,
            'triage':     triage,
            'topic':      best_topic,
            'url':        url,
            'researched': bool(result['agent_research']),
            'contact':    enriched.get('contact_name', ''),
        }
        return result

    # Run all URLs in parallel
    import os
    max_workers = int(os.getenv('SCOUT_WORKERS', '5'))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_process_url, (i + 1, url, et)): url
            for i, (url, et) in enumerate(url_items)
        }
        for future in as_completed(futures):
            res = future.result()
            if res is None:
                continue
            with lock:
                for key in (
                    'scraped', 'researched', 'scored', 'pushed',
                    'skipped_scrape_fail', 'skipped_duplicate',
                    'skipped_score_fail', 'skipped_rejected'
                ):
                    summary[key] += res[key]
                summary['agent_runs']['research'] += res.get('agent_research', 0)
                summary['agent_runs']['pitch']    += res.get('agent_pitch', 0)
                if res['triage']:
                    summary['triage_counts'][res['triage']] += 1
                if res['lead']:
                    summary['leads'].append(res['lead'])
                    processed += 1
                if processed >= max_leads:
                    logger.info(f"[ORCHESTRATOR] Max leads reached ({max_leads})")
                    for f in futures:
                        f.cancel()
                    break

    # ── Final summary ─────────────────────────────────────────────────────────
    logger.info("[ORCHESTRATOR] ====== PIPELINE COMPLETE ======")
    logger.info(f"[ORCHESTRATOR]   URLs found:          {summary['total_urls']}")
    logger.info(f"[ORCHESTRATOR]   Scraped:             {summary['scraped']}")
    logger.info(f"[ORCHESTRATOR]   Research Agent runs: {summary['agent_runs']['research']}")
    logger.info(f"[ORCHESTRATOR]   Scored:              {summary['scored']}")
    logger.info(f"[ORCHESTRATOR]   Pitch Agent runs:    {summary['agent_runs']['pitch']}")
    logger.info(f"[ORCHESTRATOR]   Pushed to Airtable:  {summary['pushed']}")
    logger.info(f"[ORCHESTRATOR]   Skipped (duplicate): {summary['skipped_duplicate']}")
    logger.info(f"[ORCHESTRATOR]   Skipped (scrape):    {summary['skipped_scrape_fail']}")
    logger.info(f"[ORCHESTRATOR]   Skipped (score):     {summary['skipped_score_fail']}")
    logger.info(f"[ORCHESTRATOR]   Skipped (rejected):  {summary['skipped_rejected']}")
    logger.info(
        f"[ORCHESTRATOR]   Triage: "
        f"RED={summary['triage_counts']['RED']} "
        f"YELLOW={summary['triage_counts']['YELLOW']} "
        f"GREEN={summary['triage_counts']['GREEN']}"
    )

    return summary


def _run_scout_agent(profile: dict, profile_path: str, settings: Settings) -> dict:
    """Scout Agent — finds URLs to process.

    Mirrors the search logic from scout.py but returns url_type_map
    for the orchestrator to process.
    """
    typed_queries = generate_search_queries(profile)
    logger.info(f"[SCOUT AGENT] Generated {len(typed_queries)} search queries")

    seed_path = str(Path(profile_path).parent.parent / 'seed_urls.json')

    # Group queries by event type, skip Podcasts (handled by Apify)
    query_groups: dict[str, list[str]] = defaultdict(list)
    for query, event_type in typed_queries:
        if event_type == 'Podcast':
            continue
        query_groups[event_type].append(query)

    url_type_map: dict[str, str] = {}
    url_type_lock = threading.Lock()

    search_args = []
    first = True
    for et, tq in query_groups.items():
        search_args.append((et, tq, seed_path if first else ''))
        first = False

    def _search_group(args: tuple) -> tuple:
        et, tq, sp = args
        return et, web_search(tq, results_per_query=20, delay=1.5, seed_urls_path=sp)

    with ThreadPoolExecutor(max_workers=len(search_args) or 1) as ex:
        for et, urls in ex.map(_search_group, search_args):
            with url_type_lock:
                for url in urls:
                    if url not in url_type_map:
                        url_type_map[url] = et
            logger.info(
                f"[SCOUT AGENT] [{et}] → "
                f"{sum(1 for v in url_type_map.values() if v == et)} URLs"
            )

    return url_type_map


def _load_profile(profile_path: str) -> dict:
    """Load speaker profile from JSON file."""
    path = Path(profile_path)
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")
    with open(path) as f:
        return json.load(f)


def _parse_date_to_iso(date_str: str) -> Optional[str]:
    """Try to parse a date string into YYYY-MM-DD format."""
    if not date_str:
        return None
    import re
    from datetime import datetime

    date_str = date_str.strip()
    date_str = re.sub(r'(\d{1,2})\s*[-–]\s*\d{1,2}', r'\1', date_str)

    formats = [
        '%B %d, %Y', '%B %d %Y',
        '%b %d, %Y', '%b %d %Y',
        '%m/%d/%Y',  '%Y-%m-%d',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None
