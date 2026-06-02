"""JobSpy-based job discovery: searches Indeed, LinkedIn, Glassdoor, ZipRecruiter.

Uses python-jobspy to scrape multiple job boards, deduplicates results,
parses salary ranges, and stores everything in the ApplyPilot database.

Search queries, locations, and filtering rules are loaded from the user's
search configuration YAML (searches.yaml) rather than being hardcoded.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from jobspy import scrape_jobs

from applypilot import config
from applypilot.database import get_connection, init_db, store_jobs

log = logging.getLogger(__name__)


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    """Parse host:port:user:pass into components."""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host,
            "port": port,
            "user": user,
            "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": passwd,
            },
        }
    elif len(parts) == 2:
        host, port = parts
        return {
            "host": host,
            "port": port,
            "user": None,
            "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    else:
        raise ValueError(
            f"Proxy format not recognized: {proxy_str}. "
            f"Expected: host:port:user:pass or host:port"
        )


# -- Retry wrapper -----------------------------------------------------------

def _scrape_with_retry(kwargs: dict, max_retries: int = 2, backoff: float = 5.0):
    """Call scrape_jobs with retry on transient failures."""
    for attempt in range(max_retries + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract accept/reject location lists from search config.

    Falls back to sensible defaults if not defined in the YAML.
    """
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs are always accepted. Non-remote jobs must match an accept
    pattern and not match a reject pattern.
    """
    if not location:
        return True  # unknown location -- keep it, let scorer decide

    loc = location.lower()

    # Remote jobs always OK
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True

    # Reject non-remote matches
    for r in reject:
        if r.lower() in loc:
            return False

    # Accept matches
    for a in accept:
        if a.lower() in loc:
            return True

    # No match -- reject unknown
    return False


# -- Site derivation from direct URLs ---------------------------------------

# Known job board domains — jobs whose direct URL still points here keep
# their original board site label and will be blocked from applying.
_BOARD_DOMAINS = {
    "indeed.com", "linkedin.com", "ziprecruiter.com", "glassdoor.com",
    "google.com", "google.jobs",
}

# Multi-tenant ATS platforms where the company name is in the subdomain
_SUBDOMAIN_ATS = {
    "myworkdayjobs.com", "myworkday.com",
    "applytojob.com",         # JazzHR
    "greenhouse.io",          # boards.greenhouse.io
    "pinpointhq.com",
    "bamboohr.com",
    "recruitee.com",
    "comeet.com",
}

# Known company → career portal URL mapping.
# When JobSpy finds a job but provides no direct apply URL, we look up
# the company here to get a working career page URL to apply on.
_KNOWN_COMPANY_URLS: dict[str, str] = {
    # Big Tech
    "microsoft": "https://careers.microsoft.com/us/en/search-results",
    "google": "https://www.google.com/about/careers/applications/jobs/results",
    "amazon": "https://www.amazon.jobs/en/search",
    "aws": "https://www.amazon.jobs/en/search",
    "meta": "https://www.metacareers.com/jobs",
    "facebook": "https://www.metacareers.com/jobs",
    # Major tech
    "apple": "https://jobs.apple.com/en-us/search",
    "netflix": "https://netflix.wd1.myworkdayjobs.com/Netflix",
    "uber": "https://uber.wd5.myworkdayjobs.com/uberCareers",
    "airbnb": "https://careers.airbnb.com/positions",
    "twitter": "https://about.twitter.com/en/careers",
    "x": "https://about.twitter.com/en/careers",
    "salesforce": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
    "nvidia": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "cisco": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers",
    "intel": "https://intel.wd1.myworkdayjobs.com/External",
    "adobe": "https://adobe.wd5.myworkdayjobs.com/external_experienced",
    "ibm": "https://www.ibm.com/careers/us-en/search",
    "oracle": "https://oracle.wd1.myworkdayjobs.com/oraclecareers",
    "dell": "https://dell.wd1.myworkdayjobs.com/External",
    "hp": "https://hp.wd5.myworkdayjobs.com/HPCareers",
    "hpe": "https://hpe.wd5.myworkdayjobs.com/hpecareers",
    "sap": "https://sap.wd1.myworkdayjobs.com/sapcareers",
    "servicenow": "https://servicenow.wd1.myworkdayjobs.com/ServiceNowCareers",
    "workday": "https://workday.wd5.myworkdayjobs.com/Workday",
    "twilio": "https://twilio.wd1.myworkdayjobs.com/Twilio",
    "databricks": "https://databricks.wd1.myworkdayjobs.com/DBX_Careers",
    "reddit": "https://reddit.wd1.myworkdayjobs.com/reddit",
    "spotify": "https://spotify.wd1.myworkdayjobs.com/Spotify",
    "slack": "https://slack.wd1.myworkdayjobs.com/Slack",
    "github": "https://github.wd1.myworkdayjobs.com/GitHub",
    "gitlab": "https://gitlab.wd1.myworkdayjobs.com/gitlab",
    "zoom": "https://zoom.wd5.myworkdayjobs.com/Zoom",
    "pagerduty": "https://pagerduty.wd1.myworkdayjobs.com/PagerDuty",
    "stripe": "https://stripe.wd1.myworkdayjobs.com/Stripe",
    "square": "https://square.wd1.myworkdayjobs.com/squarercareers",
    "block": "https://block.wd1.myworkdayjobs.com/Block",
    "palantir": "https://palantir.wd1.myworkdayjobs.com/Palantir",
    "snowflake": "https://snowflake.wd1.myworkdayjobs.com/Snowflake",
    "confluent": "https://confluent.wd1.myworkdayjobs.com/Confluent",
    "mongodb": "https://mongodb.wd1.myworkdayjobs.com/MongoDB",
    "elastic": "https://elastic.wd1.myworkdayjobs.com/elastic",
    "hashicorp": "https://hashicorp.wd1.myworkdayjobs.com/HashiCorp",
    "cloudflare": "https://cloudflare.wd1.myworkdayjobs.com/Cloudflare",
    "datadog": "https://datadog.wd1.myworkdayjobs.com/Datadog",
    "splunk": "https://splunk.wd1.myworkdayjobs.com/Splunk",
    "paloa": "https://paloa.wd1.myworkdayjobs.com/PaloAltoCareers",
    "crowdstrike": "https://crowdstrike.wd1.myworkdayjobs.com/CrowdStrike",
    "okta": "https://okta.wd1.myworkdayjobs.com/Okta",
    "unity": "https://unity.wd1.myworkdayjobs.com/Unity",
    "roblox": "https://roblox.wd1.myworkdayjobs.com/Roblox",
    "snap": "https://snap.wd1.myworkdayjobs.com/Snap",
    "pinterest": "https://pinterest.wd1.myworkdayjobs.com/Pinterest",
    "etsy": "https://etsy.wd1.myworkdayjobs.com/Etsy",
    "doordash": "https://doordash.wd1.myworkdayjobs.com/Doordash",
    "lyft": "https://lyft.wd1.myworkdayjobs.com/lyft",
    "coinbase": "https://coinbase.wd1.myworkdayjobs.com/Coinbase",
    "robinhood": "https://robinhood.wd1.myworkdayjobs.com/Robinhood",
    "instacart": "https://instacart.wd1.myworkdayjobs.com/Instacart",
    # Hardware / Semiconductor
    "amd": "https://amd.wd1.myworkdayjobs.com/AMD",
    "qualcomm": "https://qualcomm.wd1.myworkdayjobs.com/Qualcomm",
    "micron": "https://micron.wd1.myworkdayjobs.com/Micron",
    "broadcom": "https://broadcom.wd1.myworkdayjobs.com/Broadcom",
    "texas instruments": "https://ti.wd1.myworkdayjobs.com/TI_Careers",
    "applied materials": "https://amat.wd1.myworkdayjobs.com/AppliedMaterials",
    "asml": "https://asml.wd1.myworkdayjobs.com/ASML",
    # Cloud / Infrastructure
    "rackspace": "https://rackspace.wd1.myworkdayjobs.com/RackspaceCareers",
    "digitalocean": "https://digitalocean.wd1.myworkdayjobs.com/DigitalOcean",
    "fastly": "https://fastly.wd1.myworkdayjobs.com/Fastly",
    "cloudflare": "https://cloudflare.wd1.myworkdayjobs.com/Cloudflare",
    "akamai": "https://akamai.wd1.myworkdayjobs.com/Akamai",
    "netapp": "https://netapp.wd1.myworkdayjobs.com/NetApp",
    "pure storage": "https://purestorage.wd1.myworkdayjobs.com/PureStorage",
    # Financial / Fintech
    "mastercard": "https://mastercard.wd1.myworkdayjobs.com/CorporateCareers",
    "visa": "https://visa.wd1.myworkdayjobs.com/Visa",
    "paypal": "https://paypal.wd1.myworkdayjobs.com/jobs",
    "stripe": "https://stripe.wd1.myworkdayjobs.com/Stripe",
    "square": "https://square.wd1.myworkdayjobs.com/squarercareers",
    "chime": "https://chime.wd1.myworkdayjobs.com/Chime",
    "sofi": "https://sofi.wd1.myworkdayjobs.com/SoFi",
    "affirm": "https://affirm.wd1.myworkdayjobs.com/Affirm",
    "plaid": "https://plaid.wd1.myworkdayjobs.com/Plaid",
    "robinhood": "https://robinhood.wd1.myworkdayjobs.com/Robinhood",
    "upstart": "https://upstart.wd1.myworkdayjobs.com/Upstart",
    "betterment": "https://betterment.wd1.myworkdayjobs.com/Betterment",
    # Consulting
    "pwc": "https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers",
    "deloitte": "https://deloitte.wd1.myworkdayjobs.com/Deloitte",
    "accenture": "https://accenture.wd1.myworkdayjobs.com/Accenture",
    "mckinsey": "https://mckinsey.wd1.myworkdayjobs.com/McKinsey",
    "bain": "https://bain.wd1.myworkdayjobs.com/Bain",
    "boston consulting": "https://bcg.wd1.myworkdayjobs.com/BCG",
    # Automotive / Transport
    "tesla": "https://tesla.wd5.myworkdayjobs.com/Tesla",
    "rivian": "https://rivian.wd1.myworkdayjobs.com/Rivian",
    "lucid": "https://lucidmotors.wd1.myworkdayjobs.com/LucidMotors",
    "waymo": "https://waymo.wd1.myworkdayjobs.com/Waymo",
    "cruise": "https://cruise.wd1.myworkdayjobs.com/Cruise",
    # Pharma / Biotech
    "moderna": "https://modernatx.wd1.myworkdayjobs.com/M_tx",
    "pfizer": "https://pfizer.wd1.myworkdayjobs.com/Pfizer",
    "johnson & johnson": "https://jnj.wd1.myworkdayjobs.com/JNJ",
    "gsk": "https://gsk.wd1.myworkdayjobs.com/GSK",
    "merck": "https://merck.wd1.myworkdayjobs.com/Merck",
    "abbvie": "https://abbvie.wd1.myworkdayjobs.com/AbbVie",
    "amgen": "https://amgen.wd1.myworkdayjobs.com/Amgen",
    "gilead": "https://gilead.wd1.myworkdayjobs.com/Gilead",
    "bristol": "https://bms.wd1.myworkdayjobs.com/BMS",
    "regeneron": "https://regeneron.wd1.myworkdayjobs.com/Regeneron",
    "illumina": "https://illumina.wd1.myworkdayjobs.com/Illumina",
    # Telecom
    "verizon": "https://verizon.wd1.myworkdayjobs.com/Verizon",
    "att": "https://att.wd1.myworkdayjobs.com/ATT",
    "tmobile": "https://tmobile.wd1.myworkdayjobs.com/TMobile",
    "comcast": "https://comcast.wd1.myworkdayjobs.com/Comcast",
    # Retail / E-commerce
    "walmart": "https://walmart.wd1.myworkdayjobs.com/Walmart",
    "target": "https://target.wd1.myworkdayjobs.com/Target",
    "costco": "https://costco.wd1.myworkdayjobs.com/Costco",
    "home depot": "https://homedepot.wd1.myworkdayjobs.com/HomeDepot",
    "lowes": "https://lowes.wd1.myworkdayjobs.com/Lowes",
    "best buy": "https://bestbuy.wd1.myworkdayjobs.com/BestBuy",
    "shopify": "https://shopify.wd1.myworkdayjobs.com/Shopify",
    # Defense / Aerospace
    "lockheed": "https://lockheed.wd1.myworkdayjobs.com/LockheedMartin",
    "raytheon": "https://raytheon.wd1.myworkdayjobs.com/Raytheon",
    "northrop": "https://northrop.wd1.myworkdayjobs.com/NorthropGrumman",
    "boeing": "https://boeing.wd1.myworkdayjobs.com/Boeing",
    "spacex": "https://spacex.wd1.myworkdayjobs.com/SpaceX",
}


def _derive_site_from_url(apply_url: str | None, company_from_jobspy: str | None,
                           board_label: str) -> str:
    """Derive a site/company name from the direct application URL.

    Uses a chain of heuristics:
    1. If the direct URL points to a known job board domain → keep board label
    2. For multi-tenant ATS domains (Workday, JazzHR, etc.) → extract company
       from subdomain
    3. For common ATS subdomains (jobs.lever.co, jobs.ashbyhq.com) → extract
       company from subdomain or first path segment
    4. For greenhouse.io → extract company from path
    5. For grnh.se (Greenhouse short links) → fall back to JobSpy company field
    6. For standard career sites (careers.company.com, company.com/careers) →
       extract company from domain
    7. Fall back to original board label
    """
    if not apply_url:
        # No direct URL — try to use the company name from JobSpy
        # instead of falling straight back to the board label.
        if company_from_jobspy:
            cleaned = _clean_company_name(company_from_jobspy)
            if cleaned:
                return cleaned
        return board_label

    parsed = urlparse(apply_url)
    domain = parsed.netloc.lower()
    path = parsed.path.strip("/")

    # Strip www. prefix
    if domain.startswith("www."):
        domain = domain.removeprefix("www.")

    # Check if URL still points to a job board → keep original label
    for board_domain in _BOARD_DOMAINS:
        if domain == board_domain or domain.endswith("." + board_domain):
            return board_label

    # Check multi-tenant ATS where company is the subdomain prefix
    for ats_domain in _SUBDOMAIN_ATS:
        if ats_domain in domain:
            # company.ats_domain → extract company from subdomain
            company_part = domain.removesuffix(ats_domain).strip(".")
            if company_part:
                # Workday: "nvidia.wd5.myworkdayjobs.com" → company = "nvidia"
                # But there might be a ".wd5" suffix, take first part
                parts = company_part.split(".")
                result = _clean_company_name(parts[0])
                if result:
                    return result

    # Lever: jobs.lever.co/companyname
    if "jobs.lever.co" in domain:
        comp = path.split("/")[0] if path else ""
        if comp:
            return _clean_company_name(comp)

    # Ashby: jobs.ashbyhq.com/company
    if "jobs.ashbyhq.com" in domain:
        comp = path.split("/")[0] if path else ""
        if comp:
            return _clean_company_name(comp)

    # Workable
    if "workable.com" in domain and domain != "workable.com":
        # company.workable.com
        company_part = domain.removesuffix(".workable.com")
        if company_part:
            return _clean_company_name(company_part)

    # Greenhouse full URLs: boards.greenhouse.io/companyname
    if "greenhouse.io" in domain or "grnh.se" in domain:
        # Try path first
        comp = path.split("/")[0] if path and "greenhouse.io" in domain else ""
        if comp and comp != "jobs":
            return _clean_company_name(comp)
        # Fall back to JobSpy company field for grnh.se short links
        if company_from_jobspy:
            return company_from_jobspy
        return board_label

    # Standard career sites: careers.company.com, company.com/careers
    # Extract the main domain name
    domain_parts = domain.split(".")
    if len(domain_parts) >= 2:
        main_domain = domain_parts[-2].title()
        # Skip generic TLD-only names
        if _clean_company_name(main_domain):
            return main_domain

    # Last resort: use company field from JobSpy if available
    if company_from_jobspy:
        return company_from_jobspy

    return board_label


def _clean_company_name(name: str) -> str:
    """Convert a URL slug to a readable company name.

    Handles:
    - Hyphenated slugs: "data-ideology" → "Data Ideology"
    - Workday tenant prefixes: "nvidia" → "Nvidia"
    - Short company codes
    """
    # Remove workday version suffixes like "wd3", "wd5", "wd503"
    name = re.sub(r'wd\d+$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^wd\d+', '', name, flags=re.IGNORECASE)
    # Remove trailing/leading dots or hyphens
    name = name.strip(".-")
    if not name or len(name) < 2:
        return None
    # Skip generic ATS subdomains that aren't company names
    _GENERIC_SUBDOMAINS = {
        "boards", "careers", "jobs", "apply", "job-boards", "career",
        "recruiting", "employment", "hr", "staffing", "taleo",
    }
    if name.lower() in _GENERIC_SUBDOMAINS:
        return None
    # Split on hyphens, title case each part
    parts = name.split("-")
    title = " ".join(p.title() for p in parts if p)
    return title.strip() if title.strip() else None


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def store_jobspy_results(conn: sqlite3.Connection, df, source_label: str) -> tuple[int, int]:
    """Store JobSpy DataFrame results into the DB. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for _, row in df.iterrows():
        url = str(row.get("job_url", ""))
        if not url or url == "nan":
            continue

        title = str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None
        company = str(row.get("company", "")) if str(row.get("company", "")) != "nan" else None
        location_str = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None

        # Build salary string from min/max
        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = str(row.get("interval", "")) if str(row.get("interval", "")) != "nan" else ""
        currency = str(row.get("currency", "")) if str(row.get("currency", "")) != "nan" else ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = str(row.get("description", "")) if str(row.get("description", "")) != "nan" else None
        site_name = str(row.get("site", source_label))
        is_remote = row.get("is_remote", False)

        site_label = f"{site_name}"
        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        strategy = "jobspy"

        # If JobSpy gave us a full description, promote it directly
        full_description = None
        detail_scraped_at = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now

        # Extract apply URL if JobSpy provided it
        apply_url = str(row.get("job_url_direct", "")) if str(row.get("job_url_direct", "")) != "nan" else None

        # If JobSpy didn't give us a direct URL, check if we know this company's
        # career portal. This rescues jobs that would otherwise be dead ends.
        if not apply_url and company:
            known_url = _KNOWN_COMPANY_URLS.get(company.lower())
            if known_url:
                apply_url = known_url

        # Derive site from the direct URL instead of the source board
        # This way we apply on the company's own ATS, not on Indeed/LinkedIn
        site_label = _derive_site_from_url(apply_url, company, site_label)

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, "
                "full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, salary, description, location_str, site_label, strategy, now,
                 full_description, apply_url, detail_scraped_at),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    glassdoor_map: dict,
) -> dict:
    """Run a single search query and store results in DB."""
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"
    if "tier" in s:
        label += f" [tier {s['tier']}]"

    # Split sites: Glassdoor needs simplified location, others use original
    gd_location = glassdoor_map.get(s["location"], s["location"].split(",")[0])
    has_glassdoor = "glassdoor" in sites
    other_sites = [si for si in sites if si != "glassdoor"]

    all_dfs = []

    # Run non-Glassdoor sites with original location
    if other_sites:
        kwargs = {
            "site_name": other_sites,
            "search_term": s["query"],
            "location": s["location"],
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "country_indeed": defaults.get("country_indeed", "usa"),
            "verbose": 0,
        }
        if s.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in other_sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs, max_retries=max_retries)
            all_dfs.append(df)
        except Exception as e:
            log.error("[%s] (non-gd): %s", label, e)

    # Run Glassdoor separately with simplified location
    if has_glassdoor:
        gd_kwargs = {
            "site_name": ["glassdoor"],
            "search_term": s["query"],
            "location": gd_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "verbose": 0,
        }
        if s.get("remote"):
            gd_kwargs["is_remote"] = True
        if proxy_config:
            gd_kwargs["proxies"] = [proxy_config["jobspy"]]
        try:
            gd_df = _scrape_with_retry(gd_kwargs, max_retries=max_retries)
            all_dfs.append(gd_df)
        except Exception as e:
            log.error("[%s] (glassdoor): %s", label, e)

    if not all_dfs:
        log.error("[%s]: all sites failed", label)
        return {"new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0, "label": label}

    import pandas as pd
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0, "label": label}

    # Filter by location before storing
    before = len(df)
    df = df[df.apply(lambda row: _location_ok(
        str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None,
        accept_locs, reject_locs,
    ), axis=1)]
    filtered = before - len(df)

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, s["query"])

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered (location)"
    log.info(msg)

    return {"new": new, "existing": existing, "errors": 0, "filtered": filtered, "total": before, "label": label}


# -- Single query search -----------------------------------------------------

def search_jobs(
    query: str,
    location: str,
    sites: list[str] | None = None,
    remote_only: bool = False,
    results_per_site: int = 50,
    hours_old: int = 72,
    proxy: str | None = None,
    country_indeed: str = "usa",
) -> dict:
    """Run a single job search via JobSpy and store results in DB."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Search: \"%s\" in %s | sites=%s | remote=%s", query, location, sites, remote_only)

    kwargs = {
        "site_name": sites,
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "country_indeed": country_indeed,
        "verbose": 2,
    }

    if remote_only:
        kwargs["is_remote"] = True

    if proxy_config:
        kwargs["proxies"] = [proxy_config["jobspy"]]

    if "linkedin" in sites:
        kwargs["linkedin_fetch_description"] = True

    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:
        log.error("JobSpy search failed: %s", e)
        return {"error": str(e), "total": 0, "new": 0, "existing": 0}

    total = len(df)
    log.info("JobSpy returned %d results", total)

    if total == 0:
        return {"total": 0, "new": 0, "existing": 0}

    if "site" in df.columns:
        site_counts = df["site"].value_counts()
        for site, count in site_counts.items():
            log.info("  %s: %d", site, count)

    conn = init_db()
    new, existing = store_jobspy_results(conn, df, query)
    log.info("Stored: %d new, %d already in DB", new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    log.info("DB total: %d jobs, %d pending detail scrape", db_total, pending)

    return {"total": total, "new": new, "existing": existing}


# -- Full crawl (all queries x all locations) --------------------------------

def _full_crawl(
    search_cfg: dict,
    tiers: list[int] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_per_site: int = 100,
    hours_old: int = 72,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Run all search queries from search config across all locations."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    # Build search combinations from config
    queries = search_cfg.get("queries", [])
    locs = search_cfg.get("locations", [])
    defaults = search_cfg.get("defaults", {})
    glassdoor_map = search_cfg.get("glassdoor_location_map", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)

    if tiers:
        queries = [q for q in queries if q.get("tier") in tiers]
    if locations:
        locs = [loc for loc in locs if loc.get("label") in locations]

    searches = []
    for q in queries:
        for loc in locs:
            searches.append({
                "query": q["query"],
                "location": loc["location"],
                "remote": loc.get("remote", False),
                "tier": q.get("tier", 0),
            })

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Full crawl: %d search combinations", len(searches))
    log.info("Sites: %s | Results/site: %d | Hours old: %d",
             ", ".join(sites), results_per_site, hours_old)

    # Ensure DB schema is ready
    init_db()

    total_new = 0
    total_existing = 0
    total_errors = 0
    completed = 0

    for s in searches:
        result = _run_one_search(
            s, sites, results_per_site, hours_old,
            proxy_config, defaults, max_retries,
            accept_locs, reject_locs, glassdoor_map,
        )
        completed += 1
        total_new += result["new"]
        total_existing += result["existing"]
        total_errors += result["errors"]

        if completed % 5 == 0 or completed == len(searches):
            log.info("Progress: %d/%d queries done (%d new, %d dupes, %d errors)",
                     completed, len(searches), total_new, total_existing, total_errors)

    # Final stats
    conn = get_connection()
    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB",
             total_new, total_existing, total_errors, db_total)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": total_errors,
        "db_total": db_total,
        "queries": len(searches),
    }


# -- Public entry point ------------------------------------------------------

def run_discovery(cfg: dict | None = None) -> dict:
    """Main entry point for JobSpy-based job discovery.

    Loads search queries and locations from the user's search config YAML,
    then runs a full crawl across all configured job boards.

    Args:
        cfg: Override the search configuration dict. If None, loads from
             the user's searches.yaml file.

    Returns:
        Dict with stats: new, existing, errors, db_total, queries.
    """
    if cfg is None:
        cfg = config.load_search_config()

    if not cfg:
        log.warning("No search configuration found. Run `applypilot init` to create one.")
        return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    proxy = cfg.get("proxy")
    sites = cfg.get("sites")
    results_per_site = cfg.get("defaults", {}).get("results_per_site", 100)
    hours_old = cfg.get("defaults", {}).get("hours_old", 72)
    tiers = cfg.get("tiers")
    locations = cfg.get("location_labels")

    return _full_crawl(
        search_cfg=cfg,
        tiers=tiers,
        locations=locations,
        sites=sites,
        results_per_site=results_per_site,
        hours_old=hours_old,
        proxy=proxy,
    )
