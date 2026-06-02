"""
Big Tech direct API scrapers — Microsoft, Google, Amazon, Meta.
Uses public job search APIs with proper error handling.
"""
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx

from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


def _store_job(conn, title, company, location, url, description):
    try:
        conn.execute(
            """INSERT OR IGNORE INTO jobs
               (url, title, description, location, site, strategy, discovered_at, application_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (url, (title or "Untitled")[:500],
             (description or "")[:10000],
             (location or "Remote")[:200],
             company[:100], "bigtech",
             datetime.now(timezone.utc).isoformat(), url),
        )
        return conn.total_changes > 0
    except Exception as e:
        log.error("DB error: %s", e)
        return False


# ── Microsoft ────────────────────────────────────────────────────────────

def scrape_microsoft(query: str, max_results: int = 50) -> int:
    """Scrape Microsoft Careers via their search API."""
    new = 0
    params = {"q": query, "l": "en_US", "pg": 1, "pgSize": max_results}
    try:
        resp = httpx.get(
            "https://gcsservices.careers.microsoft.com/search/api/v1/search",
            params=params, headers={"User-Agent": UA},
            timeout=TIMEOUT, follow_redirects=True, verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        conn = get_connection()
        jobs = (data.get("operationResult", {})
                .get("result", {}).get("jobs", []))
        for job in jobs:
            title = job.get("title", "")
            props = job.get("properties", {})
            loc = props.get("locations", "")
            job_id = job.get("jobId", "")
            desc = props.get("description", "") or job.get("description", "")
            apply_url = f"https://careers.microsoft.com/us/en/job/{job_id}"
            if _store_job(conn, title, "Microsoft", loc, apply_url, desc):
                new += 1
        conn.commit()
        log.info("Microsoft: %d new for '%s'", new, query)
    except Exception as e:
        log.error("Microsoft error: %s", e)
    return new


# ── Google ───────────────────────────────────────────────────────────────

def scrape_google(query: str, max_results: int = 50) -> int:
    """Scrape Google Careers via their job search page."""
    new = 0
    params = {"q": query, "location": "United States", "page_size": max_results}
    try:
        resp = httpx.get(
            "https://careers.google.com/api/jobs/search",
            params=params, headers={"User-Agent": UA},
            timeout=TIMEOUT, follow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("Google API returned %d, trying fallback", resp.status_code)
            # Fallback: scrape the HTML search page
            return scrape_google_html(query, max_results)
        data = resp.json()
        conn = get_connection()
        for job in data.get("jobs", []):
            title = job.get("title", "")
            loc = ", ".join(job.get("locations", []))
            job_id = job.get("id", "")
            desc = job.get("description", "") or job.get("snippet", "")
            apply_url = f"https://www.google.com/about/careers/applications/jobs/results/{job_id}"
            if _store_job(conn, title, "Google", loc, apply_url, desc):
                new += 1
        conn.commit()
        log.info("Google: %d new for '%s'", new, query)
    except Exception as e:
        log.error("Google API error: %s", e)
        # Try fallback
        try:
            return scrape_google_html(query, max_results)
        except:
            pass
    return new


def scrape_google_html(query: str, max_results: int = 50) -> int:
    """Fallback: scrape Google Careers HTML page."""
    import re
    new = 0
    url = f"https://www.google.com/about/careers/applications/jobs/results/?q={quote_plus(query)}"
    try:
        resp = httpx.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True)
        html = resp.text
        # Try to extract JSON from script tags
        matches = re.findall(r'{\\"jobTitle\\":\\"([^\\]+)\\"[^}]+}', html)
        log.info("Google HTML fallback found %d candidates", len(matches))
    except Exception as e:
        log.error("Google HTML fallback error: %s", e)
    return new


# ── Amazon ───────────────────────────────────────────────────────────────

def scrape_amazon(query: str, max_results: int = 50) -> int:
    """Scrape Amazon Jobs via their JSON API."""
    new = 0
    params = {"search_term": query, "country": "US", "page_size": max_results}
    try:
        resp = httpx.get(
            "https://www.amazon.jobs/en/search.json",
            params=params, headers={"User-Agent": UA},
            timeout=TIMEOUT, follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        conn = get_connection()
        for job in data.get("jobs", []):
            title = job.get("title", "")
            loc = job.get("location", "")
            job_id = job.get("id", "")
            desc = job.get("description", "") or job.get("description_short", "")
            apply_url = f"https://www.amazon.jobs/en/jobs/{job_id}"
            if _store_job(conn, title, "Amazon", loc, apply_url, desc):
                new += 1
        conn.commit()
        log.info("Amazon: %d new for '%s'", new, query)
    except Exception as e:
        log.error("Amazon API error: %s", e)
    return new


# ── Meta ─────────────────────────────────────────────────────────────────

def scrape_meta(query: str, max_results: int = 50) -> int:
    """Scrape Meta Careers via their API."""
    new = 0
    params = {"q": query, "location": "United States", "limit": max_results}
    try:
        resp = httpx.get(
            "https://www.metacareers.com/api/jobs",
            params=params, headers={"User-Agent": UA},
            timeout=TIMEOUT, follow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("Meta API returned %d", resp.status_code)
            return 0
        data = resp.json()
        conn = get_connection()
        jobs = data.get("data", data.get("jobs", []))
        for job in jobs:
            title = job.get("title", "")
            loc = job.get("location", {}).get("name", "") if isinstance(job.get("location"), dict) else str(job.get("location", ""))
            job_id = job.get("id", "")
            desc = job.get("description", "")
            apply_url = f"https://www.metacareers.com/jobs/{job_id}"
            if _store_job(conn, title, "Meta", loc, apply_url, desc):
                new += 1
        conn.commit()
        log.info("Meta: %d new for '%s'", new, query)
    except Exception as e:
        log.error("Meta API error: %s", e)
    return new


# ── Run all ──────────────────────────────────────────────────────────────

DEFAULT_QUERIES = [
    "AI/ML Engineer", "Machine Learning Engineer",
    "Software Engineer", "Software Developer",
    "Backend Engineer", "Full Stack Developer",
    "Data Engineer", "DevOps Engineer",
]

SCRAPERS = [
    ("Microsoft", scrape_microsoft),
    ("Google", scrape_google),
    ("Amazon", scrape_amazon),
    ("Meta", scrape_meta),
]


def run_bigtech_discovery(queries: list[str] | None = None) -> dict:
    """Run all Big Tech scrapers for the given queries."""
    if queries is None:
        queries = DEFAULT_QUERIES

    init_db()
    total_new = 0
    errors = []

    for name, scraper in SCRAPERS:
        for q in queries:
            try:
                total_new += scraper(q)
            except Exception as e:
                errors.append(f"{name}:{q}: {e}")
                log.error("%s failed for '%s': %s", name, q, e)

    log.info("Big Tech discovery done: %d new jobs", total_new)
    return {"new": total_new, "errors": errors}
