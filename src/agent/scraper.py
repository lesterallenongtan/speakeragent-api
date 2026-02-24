"""Web scraper for conference/event pages.

Uses BeautifulSoup4 to extract event details from conference websites.
Also handles Google search query generation and execution.
"""

import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Domains to skip (job boards, PDFs, irrelevant)
SKIP_DOMAINS = {
    'linkedin.com', 'facebook.com', 'twitter.com', 'x.com',
    'instagram.com', 'youtube.com', 'indeed.com', 'glassdoor.com',
    'ziprecruiter.com', 'monster.com', 'reddit.com', 'pinterest.com',
    'tiktok.com', 'amazon.com', 'ebay.com',
}

SKIP_EXTENSIONS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx'}

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
DATE_RE = re.compile(
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
    r'Dec(?:ember)?)\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s*,?\s*\d{4}',
    re.IGNORECASE
)
LOCATION_RE = re.compile(
    r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*'
    r'([A-Z]{2}|[A-Z][a-z]+(?:\s[A-Z][a-z]+)*)'
)


def should_skip_url(url: str) -> bool:
    """Check if URL should be skipped."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace('www.', '')
    if domain in SKIP_DOMAINS:
        return True
    path = parsed.path.lower()
    for ext in SKIP_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def scrape_page(url: str, timeout: int = 10) -> Optional[dict]:
    """Scrape a conference/event page and extract structured data.

    Returns dict with keys:
        url, title, description, dates, location, emails,
        linkedin_links, has_cfp, mentions_payment, full_text
    Returns None on failure.
    """
    if should_skip_url(url):
        logger.debug(f"Skipping URL: {url}")
        return None

    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, headers=headers, timeout=timeout,
                            allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None

    try:
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Remove script/style elements
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()

        # Title
        title = ''
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
        if not title:
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True)

        # Full text (capped at 2000 chars for scoring)
        full_text = soup.get_text(separator=' ', strip=True)
        full_text_trimmed = full_text[:2000]

        # Description — look for meta description or first big paragraph
        description = ''
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc:
            description = meta_desc.get('content', '')
        if not description:
            og_desc = soup.find('meta', attrs={'property': 'og:description'})
            if og_desc:
                description = og_desc.get('content', '')
        if not description:
            # First paragraph with 50+ chars
            for p in soup.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 50:
                    description = text[:500]
                    break

        # Dates
        dates_found = DATE_RE.findall(full_text)
        event_date_str = dates_found[0] if dates_found else ''

        # Location
        location = ''
        text_lower = full_text.lower()
        if 'virtual' in text_lower or 'online' in text_lower:
            location = 'Virtual'
        else:
            loc_matches = LOCATION_RE.findall(full_text)
            if loc_matches:
                location = f"{loc_matches[0][0]}, {loc_matches[0][1]}"

        # Emails
        emails = list(set(EMAIL_RE.findall(full_text)))
        # Filter out common junk emails
        emails = [
            e for e in emails
            if not any(
                x in e.lower()
                for x in ['noreply', 'no-reply', 'example.com', 'sentry']
            )
        ]

        # LinkedIn links
        linkedin_links = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if 'linkedin.com/in/' in href:
                linkedin_links.append(href)
        linkedin_links = list(set(linkedin_links))

        # Call for speakers signal
        cfp_keywords = [
            'call for speakers', 'call for proposals', 'submit a talk',
            'speaker application', 'speaker submission',
            'become a speaker', 'speaker registration',
            'call for abstracts', 'submit abstract',
            'call for presentations',
        ]
        has_cfp = any(kw in text_lower for kw in cfp_keywords)

        # Payment signal
        pay_keywords = [
            'honorarium', 'speaker fee', 'compensation',
            'paid speaker', 'speaker stipend', 'travel reimbursement',
            'speaker payment',
        ]
        no_pay_keywords = [
            'volunteer speaker', 'unpaid', 'no compensation',
            'pro bono',
        ]
        mentions_payment = any(kw in text_lower for kw in pay_keywords)
        mentions_no_payment = any(kw in text_lower for kw in no_pay_keywords)

        return {
            'url': url,
            'title': title[:200],
            'description': description[:500],
            'event_date_raw': event_date_str,
            'location': location,
            'emails': emails[:5],
            'linkedin_links': linkedin_links[:3],
            'has_cfp': has_cfp,
            'mentions_payment': mentions_payment,
            'mentions_no_payment': mentions_no_payment,
            'full_text': full_text_trimmed,
        }
    except Exception as e:
        logger.warning(f"Failed to parse {url}: {e}")
        return None


def generate_search_queries(profile: dict) -> list[str]:
    """Generate 10 search queries from a speaker profile."""
    topics = [t['topic'] for t in profile.get('topics', [])]
    discussion_points = profile.get('discussion_points', [])
    industries = profile.get('target_industries', [])
    name = profile.get('full_name', '')

    # Ensure industries is never empty — derive from topics/bio if needed
    if not industries:
        industries = ['professional']

    # Build keyword pool from topics + discussion points
    keywords = []
    for t in topics:
        # Extract key phrase from topic title
        keywords.append(t.split(':')[0].strip())
    keywords.extend(discussion_points[:5])

    # Ensure keywords is never empty
    if not keywords:
        keywords = [name or 'speaker']

    queries = []

    # Queries 1-3: keyword + industry + "call for speakers" + 2026
    for i in range(3):
        kw = keywords[i % len(keywords)]
        ind = industries[i % len(industries)]
        queries.append(f'{kw} {ind} "call for speakers" conference 2026')

    # Queries 4-6: keyword + "keynote speaker" + 2026
    for i in range(3):
        kw = keywords[(i + 3) % len(keywords)]
        ind = industries[(i + 3) % len(industries)]
        queries.append(f'{kw} "keynote speaker" {ind} conference 2026')

    # Queries 7-9: keyword + geography + "speaking opportunity" + 2026
    geo = profile.get('target_geography', 'US')
    for i in range(3):
        kw = keywords[(i + 1) % len(keywords)]
        queries.append(f'{kw} "speaking opportunity" {geo} 2026')

    # Query 10: speaker name specific
    queries.append(
        f'"{name}" type of events "looking for speakers"'
    )

    return queries[:10]


def web_search(queries: list[str],
               results_per_query: int = 3,
               delay: float = 2.0,
               seed_urls_path: str = '') -> list[str]:
    """Search the web and collect unique URLs.

    ALWAYS includes seed URLs to guarantee a minimum set of results.
    Also tries search backends for fresh results:
    1. googlesearch-python
    2. Bing scraping via requests
    """
    import sys
    all_urls = []
    seen = set()
    seed_count = 0

    # ALWAYS load seed URLs first — these are our guaranteed floor
    if seed_urls_path:
        seed_urls = _load_seed_urls(seed_urls_path)
        seed_count = len(seed_urls)
        for u in seed_urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
        print(f"[SEARCH] Loaded {seed_count} seed URLs as guaranteed base", file=sys.stderr, flush=True)
    else:
        print("[SEARCH] WARNING: No seed_urls_path provided", file=sys.stderr, flush=True)

    # Try googlesearch-python for fresh results
    search_urls = _google_search(queries, results_per_query, delay)
    if not search_urls:
        print("[SEARCH] Google returned 0 results, trying Bing", file=sys.stderr, flush=True)
        search_urls = _bing_search(queries, results_per_query, delay)

    print(f"[SEARCH] Search engines returned {len(search_urls)} URLs", file=sys.stderr, flush=True)

    # Merge search results (deduplicated)
    for u in search_urls:
        if u not in seen:
            seen.add(u)
            all_urls.append(u)

    if not all_urls:
        print("[SEARCH] WARNING: No URLs found from any source!", file=sys.stderr, flush=True)
    else:
        print(f"[SEARCH] Total URLs to process: {len(all_urls)} ({seed_count} seed + {len(search_urls)} search)", file=sys.stderr, flush=True)

    return all_urls


def _load_seed_urls(path: str) -> list[str]:
    """Load curated URLs from a JSON seed file."""
    import json as _json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        logger.warning(f"Seed URL file not found: {path}")
        return []
    try:
        with open(p) as f:
            data = _json.load(f)
        urls = [u for u in data.get('urls', []) if not should_skip_url(u)]
        logger.info(f"Loaded {len(urls)} seed URLs")
        return urls
    except Exception as e:
        logger.error(f"Failed to load seed URLs: {e}")
        return []


def _google_search(queries: list[str],
                   results_per_query: int = 3,
                   delay: float = 2.0) -> list[str]:
    """Search via googlesearch-python."""
    try:
        from googlesearch import search as gsearch
    except ImportError:
        return []

    urls = []
    seen = set()
    for i, query in enumerate(queries):
        logger.info(f"Google [{i+1}/{len(queries)}]: {query}")
        try:
            results = list(gsearch(query, num_results=results_per_query))
            for url in results:
                if url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"Google search failed: {e}")
        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"Google search found {len(urls)} unique URLs")
    return urls


def _bing_search(queries: list[str],
                 results_per_query: int = 3,
                 delay: float = 2.0) -> list[str]:
    """Search via Bing HTML scraping (no API key needed)."""
    urls = []
    seen = set()
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    for i, query in enumerate(queries):
        logger.info(f"Bing [{i+1}/{len(queries)}]: {query}")
        try:
            resp = requests.get(
                'https://www.bing.com/search',
                params={'q': query, 'count': str(results_per_query * 2)},
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Bing returned {resp.status_code}")
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')
            # Bing organic results are in <li class="b_algo">
            count = 0
            for li in soup.find_all('li', class_='b_algo'):
                a_tag = li.find('a', href=True)
                if a_tag:
                    href = a_tag['href']
                    if (href.startswith('http') and
                            href not in seen and
                            not should_skip_url(href)):
                        seen.add(href)
                        urls.append(href)
                        count += 1
                        if count >= results_per_query:
                            break
        except Exception as e:
            logger.warning(f"Bing search failed for '{query}': {e}")

        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"Bing search found {len(urls)} unique URLs")
    return urls


# Alias for backward compatibility
google_search = web_search
