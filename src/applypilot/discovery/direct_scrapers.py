"""Direct career site scrapers for companies that don't use Workday.

Provides company-specific scrapers that hit each company's career API
or parse their job listing pages directly. More reliable than relying
on JobSpy to find these companies on Indeed/LinkedIn.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

from applypilot.database import get_connection, init_db
from applypilot import config

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json",
}


def _fetch_json(url: str, headers: dict | None = None) -> dict | list | None:
    """Fetch a URL and parse JSON response."""
    req = Request(url, headers=headers or _HEADERS)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.warning("Failed to fetch %s: %s", url[:80], e)
        return None


def _fetch_html(url: str) -> str | None:
    """Fetch a URL and return raw HTML text."""
    req = Request(url, headers=_HEADERS)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except (URLError, OSError) as e:
        log.warning("Failed to fetch HTML %s: %s", url[:80], e)
        return None


def _is_remote_location(location: str | None) -> bool:
    """Check if a location string indicates remote or broad US eligibility.

    Delegates to the shared config.is_remote_location().
    """
    return config.is_remote_location(location)


def _is_sales_job(title: str | None) -> bool:
    """Check if a job title indicates a sales/business role — filter these out.
    Delegates to the shared config.is_sales_job().
    """
    return config.is_sales_job(title)


def _resolve_url(url: str, timeout: int = 5) -> str:
    """Follow redirects to get the final URL.

    Used to detect jobs whose listing pages redirect to a generic career
    site or external ATS.  Returns the resolved URL on success, or the
    original URL on any failure (timeout, DNS, etc.) so discovery is not
    blocked by a single flaky endpoint.
    """
    import urllib.request
    for method in ('HEAD', 'GET'):
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; ApplyPilot/1.0)'},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.url
        except Exception:
            continue
    return url


def _store_job(url: str, title: str, site: str, location: str | None = None,
               apply_url: str | None = None, description: str | None = None) -> bool:
    """Store a job in the database if new. Filters out non-remote and sales jobs.

    Follows redirects and uses the *resolved* URL as the primary key so that
    multiple job listings that all bounce to the same external landing page
    (or generic career site) are deduplicated — at most one job per landing
    URL gets stored.
    """
    resolved = _resolve_url(url)
    if resolved != url:
        log.debug("URL resolved: %s -> %s", url, resolved)

    if not _is_remote_location(location):
        return False
    if _is_sales_job(title):
        return False
    if not config.is_computer_engineering_role(title):
        return False
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO jobs
               (url, title, site, strategy, discovered_at, location, application_url, full_description)
               VALUES (?, ?, ?, 'bigtech', ?, ?, ?, ?)""",
            (resolved, title, site, now, location, apply_url, description),
        )
        conn.commit()
        return conn.total_changes > 0
    except Exception as e:
        log.warning("DB insert failed: %s", e)
        return False


# ── Microsoft ──────────────────────────────────────────────────────────────

MICROSOFT_API = "https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"

def scrape_microsoft(queries: list[str] | None = None) -> dict:
    """Scrape Microsoft careers via their internal API.

    Returns dict with keys: new, existing, errors.
    """
    if queries is None:
        queries = [
            "AI Engineer", "Machine Learning Engineer",
            "Software Engineer", "ML Engineer",
            "Data Engineer", "AI Researcher",
            "Deep Learning", "Research Scientist",
        ]
    init_db()
    new = 0
    existing = 0
    errors = 0

    for query in queries:
        url = f"{MICROSOFT_API}&query={query.replace(' ', '+')}&location=United+States&start=0&rows=50"
        data = _fetch_json(url)
        if not data or not isinstance(data, dict):
            errors += 1
            continue

        positions = data.get("data", {}).get("positions", [])
        if not positions:
            continue

        for pos in positions:
            title = pos.get("name", "")
            if not title:
                continue

            pos_id = pos.get("id", "")
            job_url = f"https://apply.careers.microsoft.com/careers/job/{pos_id}"
            locations = ", ".join(pos.get("locations", [])) if pos.get("locations") else None
            dept = pos.get("department", "")
            description = f"Department: {dept}" if dept else None

            if _store_job(job_url, title, "Microsoft", locations, job_url, description):
                new += 1
            else:
                existing += 1

        # Brief pause between queries
        time.sleep(0.5)

    log.info("Microsoft scraped: %d new, %d dupes, %d errors", new, existing, errors)
    return {"new": new, "existing": existing, "errors": errors}


# ── Google ──────────────────────────────────────────────────────────────────

GOOGLE_SEARCH_URL = "https://www.google.com/about/careers/applications/jobs/results/"

def scrape_google(queries: list[str] | None = None) -> dict:
    """Scrape Google careers by parsing their server-rendered search results."""
    init_db()
    new = 0
    existing = 0
    errors = 0

    if queries is None:
        queries = [
            "AI Engineer", "Machine Learning Engineer",
            "Software Engineer", "ML Engineer",
            "Research Scientist", "AI Researcher",
        ]

    for query in queries:
        page = 1
        max_pages = 5
        while page <= max_pages:
            url = f"{GOOGLE_SEARCH_URL}?q={query.replace(' ', '+')}&page={page}"
            html = _fetch_html(url)
            if not html:
                errors += 1
                break

            # Parse job titles
            titles = re.findall(r'<h3 class="QJPWVe">([^<]+)</h3>', html)
            if not titles:
                break  # No more results

            # Parse job links (relative paths like jobs/results/NNN-slug)
            job_links = re.findall(r'href="(jobs/results/\d+[^"]*)"', html)
            # Parse locations — extract US city/state patterns
            all_locations = re.findall(
                r'([A-Z][a-z]+(?: [A-Z][a-z]+)?, [A-Z]{2}, USA)',
                html,
            )

            # Group locations: the first N locations belong to the first job, etc.
            # Each job listing seems to have ~3 location entries on average
            locs_per_job = max(1, len(all_locations) // len(titles)) if titles else 1
            locations_raw = [
                "; ".join(all_locations[i * locs_per_job:(i + 1) * locs_per_job])
                for i in range(len(titles))
            ]

            for i, title in enumerate(titles):
                if not title.strip():
                    continue
                location = locations_raw[i] if i < len(locations_raw) else ""
                job_link = job_links[i] if i < len(job_links) else ""
                job_url = f"https://www.google.com/about/careers/applications/{job_link}" if job_link else url

                if _store_job(job_url, title.strip(), "Google", location, job_url):
                    new += 1
                else:
                    existing += 1

            # Check for next page
            if f'page={page + 1}' not in html:
                break
            page += 1
            time.sleep(1)

        time.sleep(1)

    log.info("Google scraped: %d new, %d dupes, %d errors", new, existing, errors)
    return {"new": new, "existing": existing, "errors": errors}


# ── Meta ──────────────────────────────────────────────────────────────────

def scrape_meta(queries: list[str] | None = None) -> dict:
    """Scrape Meta careers — no public API available. Future target."""
    return {"new": 0, "existing": 0, "errors": 0, "note": "No public Meta API"}


# ── Netflix ───────────────────────────────────────────────────────────────
# ── OpenAI ────────────────────────────────────────────────────────────────

OPENAI_API = "https://boards.greenhouse.io/embed/jobs?board=OpenAI"


def _greenhouse_scrape(company: str, api_url: str) -> tuple[int, int, int]:
    """Scrape a Greenhouse-powered career page. Returns (new, existing, errors)."""
    url = f"{api_url}?content=true&per_page=100"
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return 0, 0, 1

    jobs_list = data.get("jobs", [])
    if not jobs_list:
        return 0, 0, 0

    new = 0
    existing = 0
    for job in jobs_list:
        title = job.get("title", "")
        if not title:
            continue

        job_id = job.get("id", "")
        if not job_id:
            continue

        job_url = job.get("absolute_url", f"https://boards.greenhouse.io/{company.lower()}/jobs/{job_id}")
        location = job.get("location", {}).get("name") if isinstance(job.get("location"), dict) else ""
        description = (job.get("content", "") or "")[:5000]

        if _store_job(job_url, title, company, location, job_url, description):
            new += 1
        else:
            existing += 1

    return new, existing, 0


def scrape_databricks(queries: list[str] | None = None) -> dict:
    """Scrape Databricks careers (Greenhouse)."""
    init_db()
    n, e, err = _greenhouse_scrape("Databricks", "https://api.greenhouse.io/v1/boards/databricks/jobs")
    return {"new": n, "existing": e, "errors": err}


# ── Runner registry ───────────────────────────────────────────────────────

SCRAPERS = {
    "Microsoft": scrape_microsoft,
    "Google": scrape_google,
    "Databricks": scrape_databricks,
}


def run_direct_scrapers(companies: list[str] | None = None) -> dict:
    """Run direct scrapers for specified companies (or all)."""
    results = {}
    total_new = 0
    total_errors = 0

    targets = [c for c in SCRAPERS if companies is None or c in companies]

    for name in targets:
        scraper = SCRAPERS[name]
        try:
            r = scraper()
            total_new += r.get("new", 0)
            total_errors += r.get("errors", 0)
            results[name] = r
        except Exception as e:
            log.error("Scraper '%s' failed: %s", name, e)
            results[name] = {"error": str(e)}
            total_errors += 1

    log.info("Direct scraping done: %d new, %d errors across %d scrapers",
             total_new, total_errors, len(targets))
    return results
