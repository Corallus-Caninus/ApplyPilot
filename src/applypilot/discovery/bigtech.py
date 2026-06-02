"""
Big Tech job discovery via JobSpy — searches Indeed/LinkedIn/Google for jobs
at Microsoft, Google, Amazon, and Meta, then routes to their ATS pages.
More reliable than direct API scrapers (which keep breaking).
"""
import logging

import numpy as np
import pandas as pd

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

# Company-specific search queries that JobSpy handles well
# These search for jobs AT these companies on Indeed/LinkedIn/Google/Glassdoor
COMPANY_QUERIES = {
    "Microsoft": [
        "Microsoft Software Engineer",
        "Microsoft AI Engineer",
        "Microsoft ML Engineer",
        "Microsoft Data Engineer",
    ],
    "Google": [
        "Google Software Engineer",
        "Google AI Engineer",
        "Google ML Engineer",
        "Google Data Engineer",
    ],
    "Amazon": [
        "Amazon Software Engineer",
        "Amazon AI Engineer",
        "Amazon ML Engineer",
        "Amazon Data Engineer",
        "AWS Software Engineer",
    ],
    "Meta": [
        "Meta Software Engineer",
        "Meta AI Engineer",
        "Meta ML Engineer",
        "Facebook AI Engineer",
    ],
}

DEFAULT_QUERIES = [
    "AI/ML Engineer", "Machine Learning Engineer",
    "Software Engineer", "Software Developer",
    "Backend Engineer", "Full Stack Developer",
    "Data Engineer", "DevOps Engineer",
]


def run_bigtech_discovery(queries: list[str] | None = None) -> dict:
    """Discover jobs at Big Tech companies via JobSpy.

    Uses JobSpy to scrape Indeed, LinkedIn, Google, and Glassdoor
    for company-specific searches. The existing site derivation logic
    routes jobs from the board site to the company's actual ATS page
    (e.g., amazon.jobs, careers.microsoft.com).
    """
    from applypilot.discovery.jobspy import _run_one_search, _load_location_config

    init_db()
    search_cfg = config.load_search_config()
    defaults = search_cfg.get("defaults", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)

    results_per_site = defaults.get("results_per_site", 50)
    hours_old = defaults.get("hours_old", 168)
    max_retries = defaults.get("max_retries", 2)

    total_new = 0
    total_existing = 0
    errors = 0

    sites = ["indeed", "linkedin", "google"]

    # Build search entries for each company
    searches = []
    for company, company_queries in COMPANY_QUERIES.items():
        for query in company_queries:
            searches.append({
                "query": query,
                "location": "Remote",
                "remote": True,
                "tier": 1,
                "company": company,
            })

    log.info("Big Tech discovery: %d company-specific searches", len(searches))

    for s in searches:
        try:
            result = _run_one_search(
                s, sites, results_per_site, hours_old,
                None, defaults, max_retries,
                accept_locs, reject_locs, {},
            )
            total_new += result.get("new", 0)
            total_existing += result.get("existing", 0)
            errors += result.get("errors", 0)
        except Exception as e:
            log.error("Search '%s' failed: %s", s["query"], e)
            errors += 1

    log.info(
        "Big Tech discovery done: %d new, %d dupes, %d errors",
        total_new, total_existing, errors,
    )
    return {"new": total_new, "existing": total_existing, "errors": errors}
