#!/usr/bin/env python3
"""Seed the autofill field_cache table from profile.json + common label patterns.

Creates the field_cache table if it doesn't exist, then populates it with
every reasonable label variation for the applicant's known profile fields.
The autofill daemon (inject_autofill.py) reads this table to auto-fill forms.

Run before the autofill daemon starts, or anytime to refresh.
"""

import json, os, sqlite3, sys

DB = os.path.expanduser("~/.applypilot/applypilot.db")
PROFILE = os.path.expanduser("~/.applypilot/profile.json")

# ── Common field label → value mappings ─────────────────────────────────
# Each profile key maps to a list of (label_variations, value_getter)
FIELD_MAP = {
    "first_name": (
        ["firstname", "first name", "first_name", "first", "given name", "givenname", "legal first name"],
        lambda p: p["personal"]["preferred_name"],
    ),
    "last_name": (
        ["lastname", "last name", "last_name", "last", "surname", "family name", "familyname", "legal last name"],
        lambda p: p["personal"]["full_name"].split()[-1],
    ),
    "email": (
        ["email", "e-mail", "email address", "emailaddress", "e-mail address"],
        lambda p: p["personal"]["email"],
    ),
    "phone": (
        ["phone", "telephone", "mobile", "cell", "phone number", "phonenumber", "telephone number",
         "primary phone", "contact phone"],
        lambda p: p["personal"]["phone"].replace("(", "").replace(")", "").replace(" ", "").replace("-", ""),
    ),
    "linkedin": (
        ["linkedin", "linkedin profile", "linkedinprofile", "linkedin profile url",
         "linkedinprofileurl", "linkedin_url", "linkedinurl"],
        lambda p: p["personal"]["linkedin_url"],
    ),
    "github": (
        ["github", "github profile", "githubprofile", "github url", "github_url", "githuburl"],
        lambda p: p["personal"]["github_url"],
    ),
    "website": (
        ["website", "portfolio", "portfolio url", "portfoliourl", "personal website", "personalwebsite",
         "url", "web site"],
        lambda p: p["personal"]["github_url"],  # fall back to GitHub
    ),
    "city": (
        ["city", "location", "current city", "currentcity"],
        lambda p: p["personal"]["city"],
    ),
    "state": (
        ["state", "province", "region"],
        lambda p: p["personal"]["province_state"],
    ),
    "country": (
        ["country", "select country"],
        lambda p: "United States",
    ),
    "zip": (
        ["zip", "zip code", "zipcode", "postal", "postal code", "postalcode"],
        lambda p: p["personal"].get("postal_code", ""),
    ),
    "work_authorization": (
        ["legally authorized", "legallyauthorized", "work authorization", "workauthorization",
         "authorized to work", "authorizedtowork", "eligible to work", "eligibletowork",
         "authorized to work in the united states"],
        lambda p: "Yes" if p["work_authorization"]["legally_authorized_to_work"] else "No",
    ),
    "sponsorship": (
        ["sponsorship", "require sponsorship", "requiresponsorship", "visa sponsorship",
         "visasponsorship", "will you require sponsorship", "employment sponsorship",
         "require employment sponsorship"],
        lambda p: "No",
    ),
    "us_citizen": (
        ["us citizen", "uscitizen", "citizen", "citizenship", "are you a us citizen",
         "what is your work authorization", "work authorization status"],
        lambda p: "US Citizen",
    ),
    "gender": (
        ["gender", "sex"],
        lambda p: "I don't wish to answer",
    ),
    "race": (
        ["race", "ethnicity", "race/ethnicity"],
        lambda p: "I don't wish to answer",
    ),
    "veteran": (
        ["veteran", "veteran status", "protected veteran"],
        lambda p: "I don't wish to answer",
    ),
    "disability": (
        ["disability", "disability status"],
        lambda p: "I don't wish to answer",
    ),
    "hispanic": (
        ["hispanic", "latino", "hispanic/latino"],
        lambda p: "I don't wish to answer",
    ),
    "salary_expectation": (
        ["salary", "expected salary", "salary expectation", "desired salary",
         "compensation", "expected compensation"],
        lambda p: f"${p['compensation']['salary_expectation']}",
    ),
    "currently_employed": (
        ["currently employed", "currentlyemployed", "current employer", "currentemployer"],
        lambda p: "Yes",
    ),
    "felony": (
        ["felony", "convicted of a felony", "criminal history", "criminalconviction",
         "have you ever been convicted"],
        lambda p: "No",
    ),
    "age_18": (
        ["at least 18", "over 18", "18 years", "18 years of age", "are you 18",
         "minimum age"],
        lambda p: "Yes",
    ),
    "background_check": (
        ["background check", "backgroundcheck", "consent to background"],
        lambda p: "Yes",
    ),
    "how_heard": (
        ["how did you hear", "how did you learn", "how you heard", "source",
         "referred by", "referral source", "how did you find"],
        lambda p: "Online Job Board",
    ),
    "previously_employed": (
        ["previously employed", "previouslyemployed", "worked here before",
         "worked at", "former employee"],
        lambda p: "No",
    ),
    "willing_to_relocate": (
        ["willing to relocate", "relocate", "relocation"],
        lambda p: "No",
    ),
}

# Also add raw field values without label mapping for direct key matching
ADDITIONAL = {
    "josh": "Josh",
    "ward": "Ward",
    "josh n ward": "Joshua N. Ward",
    "joshua n ward": "Joshua N. Ward",
    "joshua ward": "Joshua Ward",
    "exeter": "Exeter",
    "california": "California",
    "united states": "United States",
    "5596235896": "5596235896",
    "us citizen": "US Citizen",
    "online job board": "Online Job Board",
    "online": "Online Job Board",
    "linkedin": "LinkedIn",
    "github": "GitHub",
    "male": "I don't wish to answer",
    "female": "I don't wish to answer",
    "decline to self-identify": "I don't wish to answer",
    "prefer not to say": "I don't wish to answer",
    "no": "No",
    "yes": "Yes",
}


def seed():
    if not os.path.exists(DB):
        print(f"[seed] DB not found: {DB}")
        return 0

    if not os.path.exists(PROFILE):
        print(f"[seed] Profile not found: {PROFILE}")
        return 0

    with open(PROFILE) as f:
        profile = json.load(f)

    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS field_cache (label TEXT PRIMARY KEY, value TEXT, created_at TEXT, updated_at TEXT, source_session TEXT)")
    # Add missing columns for existing databases
    for _col in ('created_at', 'updated_at', 'source_session'):
        try:
            conn.execute(f"ALTER TABLE field_cache ADD COLUMN {_col} TEXT")
        except Exception:
            pass
    existing = set()
    for row in conn.execute("SELECT label FROM field_cache"):
        existing.add(row[0])

    count = 0
    # Seed from FIELD_MAP
    for key, (labels, getter) in FIELD_MAP.items():
        try:
            val = getter(profile)
        except Exception:
            continue
        if not val:
            continue
        val = str(val).strip()
        for label in labels:
            norm = label.lower().replace("*", "").replace(" ", "")
            if norm not in existing:
                conn.execute(
                    "INSERT OR IGNORE INTO field_cache (label, value) VALUES (?, ?)",
                    (norm, val),
                )
                count += 1
                existing.add(norm)

    # Seed from ADDITIONAL direct value mappings
    for norm, val in ADDITIONAL.items():
        if norm not in existing:
            conn.execute(
                "INSERT OR IGNORE INTO field_cache (label, value) VALUES (?, ?)",
                (norm, val),
            )
            count += 1
            existing.add(norm)

    # Also derive "full name" from first + last
    first = profile["personal"]["preferred_name"]
    last = profile["personal"]["full_name"].split()[-1]
    full_variants = [
        f"{first} {last}",
        f"{profile['personal']['preferred_name']} {last}",
        profile["personal"]["full_name"],
    ]
    for fv in full_variants:
        for label in ["fullname", "full name", "full_name", "name", "your name", "legal name", "legalname"]:
            if label not in existing:
                conn.execute(
                    "INSERT OR IGNORE INTO field_cache (label, value) VALUES (?, ?)",
                    (label, fv),
                )
                count += 1
                existing.add(label)

    conn.commit()

    # Show results
    cur = conn.execute("SELECT COUNT(*) FROM field_cache")
    total = cur.fetchone()[0]
    conn.close()
    print(f"[seed] Seeded {count} new entries — {total} total in field_cache")
    return count


if __name__ == "__main__":
    n = seed()
    if n:
        print("[seed] Autofill cache ready — restart the autofill daemon if running")
