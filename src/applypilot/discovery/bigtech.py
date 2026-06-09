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
    "Netflix": [
        "Netflix Software Engineer",
        "Netflix AI Engineer",
        "Netflix ML Engineer",
        "Netflix Data Engineer",
    ],
    "OpenAI": [
        "OpenAI Software Engineer",
        "OpenAI AI Engineer",
        "OpenAI ML Engineer",
    ],
    "Anthropic": [
        "Anthropic Software Engineer",
        "Anthropic AI Engineer",
        "Anthropic ML Engineer",
    ],
    "Databricks": [
        "Databricks Software Engineer",
        "Databricks AI Engineer",
        "Databricks ML Engineer",
    ],
    "Snowflake": [
        "Snowflake Software Engineer",
        "Snowflake Data Engineer",
    ],
    "IBM": [
        "IBM AI Engineer",
        "IBM Software Engineer",
        "IBM ML Engineer",
        "IBM Data Engineer",
    ],
    "Oracle": [
        "Oracle Software Engineer",
        "Oracle AI Engineer",
        "Oracle ML Engineer",
    ],
    "Qualcomm": [
        "Qualcomm Software Engineer",
        "Qualcomm AI Engineer",
        "Qualcomm ML Engineer",
    ],
    "ARM": [
        "ARM Software Engineer",
        "ARM AI Engineer",
    ],
    "Broadcom": [
        "Broadcom Software Engineer",
        "Broadcom AI Engineer",
    ],
    "Tesla": [
        "Tesla Software Engineer",
        "Tesla AI Engineer",
        "Tesla ML Engineer",
    ],
    "Cisco": [
        "Cisco Software Engineer",
        "Cisco AI Engineer",
        "Cisco ML Engineer",
        "Cisco Data Engineer",
    ],
    "AMD": [
        "AMD AI Engineer",
        "AMD Software Engineer",
        "AMD ML Engineer",
    ],
}

DEFAULT_QUERIES = [
    "AI/ML Engineer", "Machine Learning Engineer",
    "Software Engineer", "Software Developer",
    "Backend Engineer", "Full Stack Developer",
    "Data Engineer", "DevOps Engineer",
]


def run_bigtech_discovery(queries: list[str] | None = None) -> dict:
    """Discover jobs at Big Tech companies via direct API scrapers.

    Uses company-specific API scrapers for Microsoft, Google, Databricks, etc.
    Companies without a direct scraper (Meta, Netflix, OpenAI, Anthropic, Snowflake)
    remain as future targets — noted in COMPANY_QUERIES for reference.
    """
    init_db()
    total_new = 0
    total_existing = 0
    errors = 0

    # Run direct API scrapers
    try:
        from applypilot.discovery.direct_scrapers import run_direct_scrapers
        direct = run_direct_scrapers()
        for company, result in direct.items():
            cn = result.get("new", 0)
            ce = result.get("existing", 0)
            cerr = result.get("errors", 0)
            total_new += cn
            total_existing += ce
            errors += cerr
            if cn:
                log.info("  '%s': %d new jobs via direct API", company, cn)
    except Exception as e:
        log.error("Direct scrapers failed: %s", e)
        errors += 1

    # Run Workday scraper for bigtech companies (Cisco, AMD, NVIDIA, etc.)
    try:
        from applypilot.discovery.workday import run_workday_discovery
        log.info("  Scraping bigtech Workday employers...")
        wd = run_workday_discovery(workers=1)
        wn = wd.get("new", 0)
        we = wd.get("existing", 0)
        total_new += wn
        total_existing += we
        if wn:
            log.info("  Workday: %d new jobs from bigtech employers", wn)
        # Tag Workday jobs from bigtech companies as 'bigtech' strategy
        from applypilot.database import get_connection
        tech_sites = ("Cisco", "NVIDIA", "Intel", "AMD", "ARM", "Qualcomm",
                      "Oracle", "IBM", "Adobe", "Salesforce", "Netflix",
                      "ServiceNow", "DocuSign", "Uber", "Workday")
        conn = get_connection()
        placeholders = ",".join("?" * len(tech_sites))
        conn.execute(f"""
            UPDATE jobs SET strategy = 'bigtech'
            WHERE strategy = 'workday_api' AND site IN ({placeholders})
              AND apply_status IS NULL
        """, list(tech_sites))
        conn.commit()
        tagged = conn.total_changes
        if tagged:
            log.info("  Tagged %d Workday jobs as 'bigtech' strategy", tagged)
        # Remove non-remote and sales jobs from bigtech
        removed = conn.execute("""
            DELETE FROM jobs WHERE strategy = 'bigtech' AND apply_status IS NULL
            AND ((location IS NULL OR location NOT LIKE '%Remote%'
                  AND location NOT LIKE '%United States%'
                  AND location NOT LIKE '%Multiple Location%')
                 OR title LIKE '%Account Executive%'
                 OR title LIKE '%Sales%'
                 OR title LIKE '%Account Manager%'
                 OR title LIKE '%Business Development%'
                 OR title LIKE '%Customer Success%'
                 OR title LIKE '%Partner Manager%'
                 OR title LIKE '%Channel Sales%'
                 OR title LIKE '%Sales Director%'
                 OR title LIKE '%Growth Manager%'
                 OR title LIKE '%Go-to-Market%'
                 OR title LIKE '%GTM%'
                 OR title LIKE '%Commercial Director%')
        """).rowcount
        conn.commit()
        if removed:
            log.info("  Removed %d non-remote jobs from bigtech queue", removed)
    except Exception as e:
        log.error("Workday scraper failed: %s", e)
        errors += 1

    log.info("Big Tech discovery done: %d new, %d dupes, %d errors",
             total_new, total_existing, errors)
    return {"new": total_new, "existing": total_existing, "errors": errors}
