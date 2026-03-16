# Podcast Matching Strategy — SpeakerAgent.AI

## Approach & Research Process

After reviewing the existing codebase, I found that the scout pipeline
already delegates podcast queries to **Apify** (`src/agent/scout.py`).
However, Apify is used as a general web scraper — it doesn't provide
structured podcast metadata like audience size, episode frequency, or
guest history that would help score a podcast as a good match for a
speaker.

I researched available podcast APIs and data sources using Google and
AI assistance to understand what's available, then designed a matching
approach that builds on the existing Apify integration rather than
replacing it. The goal was to fit naturally into the existing
SpeakerAgent.AI scout pipeline with as little new infrastructure as
possible.

---

## Why Podcasts Matter for SpeakerAgent.AI

Conference leads take months to convert — a speaker submits a CFP,
waits for selection, then waits for the event date. Podcast guest
appearances can be booked in weeks, building the speaker's audience
and credibility much faster.

For a speaker like Dr. Leigh Vinocur, appearing on healthcare and
emergency medicine podcasts directly reaches her target audience and
creates a pipeline of future conference opportunities through increased
visibility. Podcasts are also a lower barrier to entry — hosts are
actively looking for guests, whereas conference organizers receive
hundreds of CFP submissions.

---

## 1. How I'd Source Podcast Data

### What's already in the codebase — Apify

The existing scout already uses Apify for podcast discovery
(`src/agent/scout.py`). Apify is a web scraping platform that can
crawl podcast directories like:

- **PodMatch** (`podmatch.com`) — already in `config/seed_urls.json`
- **Podcast Guests** (`podcastguests.com`) — already in `config/seed_urls.json`
- **RadioGuestList** (`radioguestlist.com`) — already in `config/seed_urls.json`

These sites list podcasts actively looking for guests — which means
higher intent than cold outreach to any random show.

**Limitation of Apify alone:** It scrapes web pages but doesn't return
structured data like episode count, listener numbers, or guest history.
We get a URL and some text — but not enough signals to score a match
confidently.

---

### What I'd add — Listen Notes API

**Listen Notes** (https://www.listennotes.com/api/) is the largest
podcast search engine with 3M+ podcasts. Unlike Apify which scrapes
pages, Listen Notes returns structured JSON with the exact fields
we need for scoring:

- `listen_score` — popularity score 0–100
- `total_episodes` — how active the show is
- `description` — what topics they cover
- `language` and `country` — for geography targeting
- `email` — direct contact for outreach

**Why Listen Notes over other options:**
- Free tier gives 100 requests/day — enough to prototype
- No scraping needed — clean structured API response
- Already widely used in the podcasting industry
- Podchaser (alternative) requires manual approval for API access

```python
import requests

def search_podcasts(query: str, api_key: str) -> list:
    """Search for podcasts using Listen Notes API."""
    resp = requests.get(
        "https://listen-api.listennotes.com/api/v2/search",
        headers={"X-ListenAPI-Key": api_key},
        params={
            "q": query,
            "type": "podcast",
            "language": "English",
            "len_min": 20,       # min episode length in minutes
            "sort_by_date": 0,   # sort by relevance
        }
    )
    return resp.json().get("results", [])
```

---

### RSS Feeds as a Supplement

Every podcast has a public RSS feed. Once we find a podcast via
Apify or Listen Notes, we can parse its RSS feed to extract:

- Recent episode titles and descriptions → what topics they cover
- Guest names mentioned in episodes → who they typically book
- Publishing frequency → is the show still active?

```python
import feedparser

def parse_podcast_rss(rss_url: str) -> dict:
    """Extract structured data from a podcast RSS feed."""
    feed = feedparser.parse(rss_url)
    return {
        "title": feed.feed.get("title"),
        "description": feed.feed.get("description"),
        "recent_episodes": [
            {
                "title": e.get("title"),
                "summary": e.get("summary", "")[:300],
                "published": e.get("published"),
            }
            for e in feed.entries[:10]  # last 10 episodes
        ]
    }
```

---

## 2. How I'd Match a Speaker's Expertise to Relevant Shows

The matching approach reuses the same pattern as the existing conference
scout — a scored match (0–100) with RED/YELLOW/GREEN triage.

### Step 1 — Generate Search Queries from Speaker Profile

The existing scout already generates search queries from the speaker's
topics and keywords (`src/agent/scout.py`). The same logic applies
for podcasts — we just change the query format:

```python
def build_podcast_queries(profile: dict) -> list:
    """Generate podcast search queries from speaker profile."""
    queries = []
    for topic in profile.get("topics", []):
        queries.append(f'{topic["title"]} podcast guest expert')
        queries.append(f'{topic["title"]} podcast "looking for guests"')
    for keyword in profile.get("niche_keywords", []):
        queries.append(f'{keyword} podcast interview')
    return queries
```

For Dr. Leigh Vinocur this would generate queries like:
- `"emergency medicine podcast guest expert"`
- `"women in medicine podcast looking for guests"`
- `"ER physician podcast interview"`

---

### Step 2 — Score Each Podcast

A multi-signal scoring system similar to the existing `Match Score`:

```python
def score_podcast_match(podcast: dict, speaker: dict) -> int:
    """Score how well a podcast matches a speaker. Returns 0-100."""
    score = 0
    podcast_text = f"""
        {podcast.get('title', '')}
        {podcast.get('description', '')}
        {podcast.get('recent_episode_topics', '')}
    """.lower()

    # Signal 1: Topic overlap (40 points max)
    for topic in speaker.get("topics", []):
        topic_words = topic["title"].lower().split()
        if any(word in podcast_text for word in topic_words):
            score += 10

    # Signal 2: Keyword match (20 points max)
    for keyword in speaker.get("niche_keywords", [])[:4]:
        if keyword.lower() in podcast_text:
            score += 5

    # Signal 3: Audience size via listen_score (20 points max)
    listen_score = podcast.get("listen_score", 0)
    score += min(int(listen_score / 5), 20)

    # Signal 4: Show is still actively publishing (10 points)
    if podcast.get("is_active", False):
        score += 10

    # Signal 5: Episode length suggests interview format (10 points)
    avg_length = podcast.get("avg_episode_length_sec", 0) / 60
    if avg_length >= 20:  # 20+ min episodes = likely interview format
        score += 10

    return min(score, 100)
```

### Triage Rules (same as existing conference leads)

| Score | Triage | Action |
|---|---|---|
| 65–100 | RED 🔴 | Strong match — outreach immediately |
| 35–64 | YELLOW 🟡 | Moderate match — worth considering |
| 0–34 | GREEN 🟢 | Weak match — low priority |

---

### Step 3 — AI Scoring with Claude (when credits available)

When Claude API credits are available, use the same pattern as
`src/agent/scoring.py` for deeper semantic matching:

```python
def ai_score_podcast(podcast: dict, speaker: dict, api_key: str) -> dict:
    """Use Claude to score podcast match when API credits available."""
    prompt = f"""
You are a podcast booking expert. Score how well this speaker matches
this podcast for a guest appearance.

SPEAKER:
- Name: {speaker['full_name']}
- Topics: {', '.join(t['title'] for t in speaker['topics'])}
- Credentials: {speaker['credentials']}
- Book: {speaker.get('book_title', 'N/A')}

PODCAST:
- Name: {podcast['title']}
- Description: {podcast['description']}
- Recent episode topics: {podcast.get('recent_episode_topics', 'N/A')}

Return JSON only:
{{
  "score": <0-100>,
  "triage": <"RED"|"YELLOW"|"GREEN">,
  "suggested_angle": "<one sentence pitch angle>",
  "hook": "<personalized 2-3 sentence outreach hook>"
}}
"""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    import json
    return json.loads(response.content[0].text)
```

---

## 3. Database Schema

New Airtable table: **`Podcasts`** — mirrors the existing `Conferences`
table structure so the same frontend components work for both.

| Field Name | Field Type | Purpose |
|---|---|---|
| `Podcast Name` | Single line text | Name of the show |
| `RSS URL` | URL | For parsing episode history |
| `Website` | URL | Show website |
| `Host Name` | Single line text | Who to contact |
| `Contact Email` | Email | Direct outreach email |
| `Contact LinkedIn` | URL | Host LinkedIn profile |
| `Listen Score` | Number | Popularity 0–100 from Listen Notes |
| `Avg Episode Length` | Number | In minutes |
| `Recent Guest Topics` | Long text | Extracted from RSS feed |
| `Date Found` | Date | When the scout found this |
| `Lead Triage` | Single line text | RED / YELLOW / GREEN |
| `Match Score` | Number | 0–100 match score |
| `Lead Status` | Single line text | New / Contacted / Replied / Booked / Passed |
| `The Hook` | Long text | AI-generated outreach opener |
| `CTA` | Long text | What the speaker should do next |
| `Suggested Angle` | Single line text | Recommended pitch angle |
| `speaker_id` | Single line text | Links to Speakers table |
| `persona_id` | Single line text | Links to Speaker_Persona table |
| `Type` | Single line text | Always "Podcast" |

---

## 4. Architecture — How it Fits into the Existing Codebase

The podcast scout follows the exact same pipeline as the conference
scout so existing infrastructure is reused:

```
scripts/run_podcast_scout.py         ← triggers the pipeline
        ↓
src/agent/podcast_scout.py           ← main matching pipeline
  ├── Apify (existing)               ← scrapes podcast directories
  ├── Listen Notes API (new)         ← structured podcast metadata
  └── RSS parser (new)               ← episode/guest history
        ↓
src/agent/scoring.py (existing)      ← reuse scoring logic
src/agent/pitch.py (existing)        ← reuse hook/CTA generation
        ↓
src/api/airtable.py (existing)       ← push to Podcasts table
        ↓
GET /api/podcasts (new endpoint)     ← serve to frontend
        ↓
Next.js frontend                     ← reuse LeadsTable component
```

### New files needed

```
scripts/
  run_podcast_scout.py        ← trigger script (mirrors run_scout.py)

src/agent/
  podcast_scout.py            ← main pipeline (mirrors scout.py)
  podcast_scraper.py          ← Listen Notes + RSS parser
```

### Existing files to reuse unchanged

```
src/agent/scoring.py          ← same scoring logic
src/agent/pitch.py            ← same hook/CTA generation
src/api/airtable.py           ← same Airtable client
src/app/components/leads-table.tsx   ← same frontend table
```

---

## 5. Example Match for Dr. Leigh Vinocur

To make this concrete — here's how the matching would work for one
real podcast:

**Target podcast:** The Curbsiders Internal Medicine Podcast

| Signal | Value | Points |
|---|---|---|
| Topic overlap: "medicine" in description | ✅ | +10 |
| Topic overlap: "emergency" in episodes | ✅ | +10 |
| Keyword: "physician" in description | ✅ | +5 |
| Listen score: 72 → 72/5 = 14 points | ✅ | +14 |
| Still actively publishing | ✅ | +10 |
| Avg episode 45 min (interview format) | ✅ | +10 |
| **Total** | | **59/100 → YELLOW** |

Generated hook:
> *"The Curbsiders covers exactly the intersection of clinical practice
> and real-world decision-making that Dr. Leigh Vinocur has spent 30
> years mastering in the ER. Her book 'Never Let Them See You Sweat'
> and her approach to triage-as-life-strategy would give your listeners
> a completely fresh perspective on staying calm under pressure."*

---

## 6. Biggest Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Podcast contact info is hard to find | Parse RSS feed for email + check website contact page |
| Listen Notes free tier only 100 req/day | Cache results in Airtable — only fetch new podcasts |
| Apify scraping gets blocked | Rotate user agents + add delays between requests |
| Low match quality without Claude credits | Keyword scorer still works as fallback (same as conferences) |
| Duplicate podcasts across scout runs | Same deduplication logic as `lead_exists()` in airtable.py |

---

## Summary

This strategy extends the existing SpeakerAgent.AI scout pipeline to
find podcast guest opportunities alongside conference leads. The key
additions are:

1. **Listen Notes API** — structured podcast metadata that Apify alone
   can't provide
2. **RSS parsing** — episode history to understand what topics a show
   covers and who they typically book
3. **`Podcasts` Airtable table** — mirrors `Conferences` so the same
   frontend works for both
4. **`podcast_scout.py`** — a new agent that reuses the existing
   scoring, pitch, and Airtable infrastructure

The approach is deliberately conservative — it reuses as much existing
code as possible and introduces only what's genuinely needed to make
podcast matching work.
