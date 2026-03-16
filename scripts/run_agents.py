"""Run the multi-agent pipeline for SpeakerAgent.AI.

This script triggers the full Scout → Research → Pitch → Orchestrator
pipeline for a speaker profile.

Usage:
    python scripts/run_agents.py
    python scripts/run_agents.py --speaker-id leigh_vinocur
    python scripts/run_agents.py --dry-run
    python scripts/run_agents.py --no-research   (skip Research Agent, faster)
    python scripts/run_agents.py --max-leads 10
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings
from src.agent.orchestrator import run_pipeline

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Run the SpeakerAgent.AI multi-agent pipeline'
    )
    parser.add_argument(
        '--speaker-id',
        default='leigh_vinocur',
        help='Speaker ID to run pipeline for (default: leigh_vinocur)'
    )
    parser.add_argument(
        '--profile-path',
        default=None,
        help='Path to speaker profile JSON (default: config/speaker_profiles/{speaker_id}.json)'
    )
    parser.add_argument(
        '--max-leads',
        type=int,
        default=None,
        help='Maximum leads to push to Airtable'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run pipeline without pushing to Airtable'
    )
    parser.add_argument(
        '--no-research',
        action='store_true',
        help='Skip Research Agent (faster but less thorough)'
    )

    args = parser.parse_args()

    # Resolve profile path
    profile_path = args.profile_path or (
        f'config/speaker_profiles/{args.speaker_id}.json'
    )

    if not Path(profile_path).exists():
        logger.error(f"Profile not found: {profile_path}")
        logger.error("Run: python scripts/seed_speaker.py first")
        sys.exit(1)

    # Validate settings
    try:
        settings = Settings()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Print pipeline info
    print("\n" + "="*60)
    print("  SpeakerAgent.AI — Multi-Agent Pipeline")
    print("="*60)
    print(f"  Speaker ID:      {args.speaker_id}")
    print(f"  Profile:         {profile_path}")
    print(f"  Max leads:       {args.max_leads or 'unlimited'}")
    print(f"  Dry run:         {args.dry_run}")
    print(f"  Research Agent:  {'disabled' if args.no_research else 'enabled'}")
    print(f"  Claude API:      {'configured' if settings.CLAUDE_API_KEY else 'MISSING'}")
    print("="*60)
    print("\n  Agents running in this order:")
    print("  1. Scout Agent    — finds conference URLs")
    print("  2. Research Agent — enriches each URL with deeper context")
    print("  3. Score Agent    — scores lead quality 0-100")
    print("  4. Verify Agent   — validates lead is real and upcoming")
    print("  5. Pitch Agent    — generates personalized hook + CTA")
    print("  6. Push           — saves to Airtable")
    print("\n" + "="*60 + "\n")

    # Run the pipeline
    try:
        summary = run_pipeline(
            profile_path=profile_path,
            speaker_id=args.speaker_id,
            max_leads=args.max_leads,
            dry_run=args.dry_run,
            enable_research=not args.no_research,
        )
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        sys.exit(1)

    # Print final summary
    print("\n" + "="*60)
    print("  PIPELINE COMPLETE")
    print("="*60)
    print(f"  URLs found:           {summary.get('total_urls', 0)}")
    print(f"  Successfully scraped: {summary.get('scraped', 0)}")
    print(f"  Research Agent runs:  {summary.get('agent_runs', {}).get('research', 0)}")
    print(f"  Scored:               {summary.get('scored', 0)}")
    print(f"  Pitch Agent runs:     {summary.get('agent_runs', {}).get('pitch', 0)}")
    print(f"  Pushed to Airtable:   {summary.get('pushed', 0)}")
    print(f"  Skipped (duplicate):  {summary.get('skipped_duplicate', 0)}")
    print(f"  Skipped (scrape):     {summary.get('skipped_scrape_fail', 0)}")
    print(f"  Skipped (rejected):   {summary.get('skipped_rejected', 0)}")

    triage = summary.get('triage_counts', {})
    print(f"\n  Triage breakdown:")
    print(f"    🔴 RED    (hot):  {triage.get('RED', 0)}")
    print(f"    🟡 YELLOW (warm): {triage.get('YELLOW', 0)}")
    print(f"    🟢 GREEN  (cool): {triage.get('GREEN', 0)}")
    print("="*60 + "\n")

    # Save summary to file for inspection
    summary_path = f'config/pipeline_summary_{args.speaker_id}.json'
    with open(summary_path, 'w') as f:
        json.dump({k: v for k, v in summary.items() if k != 'leads'}, f, indent=2)
    print(f"  Summary saved to: {summary_path}\n")

    return 0 if summary.get('pushed', 0) > 0 or args.dry_run else 1


if __name__ == '__main__':
    sys.exit(main())
