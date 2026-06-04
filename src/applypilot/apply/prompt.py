"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony: No",
        "Previously Worked Here: No",
        "How Heard: Online Job Board",
    ])

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    # Build the list of acceptable cities for hybrid/onsite
    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- "Remote" or "work from anywhere" -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" in any city outside the list above with NO remote option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if str(floor).isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Compute example hourly rates at 3 salary levels
    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY (think, don't just copy) ==
${floor} {currency} is the FLOOR. Never go below it. But don't always use it either.

Decision tree:
1. Job posting shows a range (e.g. "$120K-$160K")? -> Answer with the MIDPOINT ($140K).
2. Title says Senior, Staff, Lead, Principal, Architect, or level II/III/IV? -> Minimum $110K {currency}. Use midpoint of posted range if higher.
3. {convert_line}
4. No salary info anywhere? -> Use ${floor} {currency}.
5. Asked for a range? -> Give posted midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location/relocation: lives in {city}, cannot relocate
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only

Skills and tools -> be confident. This candidate is a {target_role} with {years} years experience. If the question asks "Do you have experience with [tool]?" and it's in the same domain (DevOps, backend, ML, cloud, automation), answer YES. Software engineers learn tools fast. Don't sell short.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real person.

EEO/demographics -> "Decline to self-identify" or "Prefer not to say" for everything.

| Field                      | Best answer                              |
|----------------------------|------------------------------------------|
| "How Did You Hear" / Source| "Online Job Board" or "LinkedIn"         |
| Referral / Employee Refer  | "No"                                     |
| Sponsorship / Visa         | "No" / "Not required"                    |
| Work Authorization         | "US Citizen" / "Authorized to work"      |
| Previously employed here   | "No"                                     |
| Felony / Criminal          | "No"                                     |
| Security Clearance         | "None"                                   |
| LinkedIn / Portfolio       | Your profile URL from above              |
| Relocation / Willing to   | "Yes" (for remote/hybrid)                |
| Travel %                   | "10%" or "25%"                           |
| Language / Spanish         | "Basic" or skip if optional              |
| Salary expectation         | Use SALARY section above                 |
| Unfamiliar dropdown        | Pick closest match, best guess, move on  |"""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    auth_info = work_auth.get("legally_authorized_to_work", "")
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = "Work auth: Answer truthfully from profile."
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    name_rule = f'Name: Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")

    return f"""== CAPTCHA ==
You solve CAPTCHAs via the CapSolver REST API. No browser extension. You control the entire flow.
API key: {capsolver_key or 'NOT CONFIGURED — skip to MANUAL FALLBACK for all CAPTCHAs'}
API base: https://api.capsolver.com

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE (createTask -> poll -> inject) with the CapSolver API
3. ONLY go to MANUAL FALLBACK if CapSolver returns errorId > 0
Do NOT skip the API call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the API first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{{{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{{{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}}}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 2. Cloudflare Turnstile
  if (!r.type) {{{{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{{{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{{{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}}}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{{{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{{{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{{{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}}}
    }}}}
  }}}}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{{{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{{{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{{{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{{{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{{{
    const el = document.querySelector('[data-pkey]');
    if (el) {{{{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}}}
  }}}}
  if (r.type) {{{{ r.url = url; return r; }}}}
  return null;
}}}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Three steps: createTask -> poll -> inject. Do each as a separate browser_evaluate call.

STEP 1 -- CREATE TASK (copy this exactly, fill in the 3 placeholders):
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/createTask', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      task: {{{{
        type: 'TASK_TYPE',
        websiteURL: 'PAGE_URL',
        websiteKey: 'SITE_KEY'
      }}}}
    }}}})
  }}}});
  return await r.json();
}}}}

TASK_TYPE values (use EXACTLY these strings):
  hcaptcha     -> HCaptchaTaskProxyLess
  recaptchav2  -> ReCaptchaV2TaskProxyLess
  recaptchav3  -> ReCaptchaV3TaskProxyLess
  turnstile    -> AntiTurnstileTaskProxyLess
  funcaptcha   -> FunCaptchaTaskProxyLess

PAGE_URL = the url from detect result. SITE_KEY = the sitekey from detect result.
For recaptchav3: add "pageAction": "submit" to the task object (or the actual action found in page scripts).
For turnstile: add "metadata": {{"action": "...", "cdata": "..."}} if those were in detect result.

Response: {{"errorId": 0, "taskId": "abc123"}} on success.
If errorId > 0 -> CAPTCHA SOLVE failed. Go to MANUAL FALLBACK.

STEP 2 -- POLL (replace TASK_ID with the taskId from step 1):
Loop: browser_wait_for time: 3, then run:
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/getTaskResult', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      taskId: 'TASK_ID'
    }}}})
  }}}});
  return await r.json();
}}}}

- status "processing" -> wait 3s, poll again. Max 10 polls (30s).
- status "ready" -> extract token:
    reCAPTCHA: solution.gRecaptchaResponse
    hCaptcha:  solution.gRecaptchaResponse
    Turnstile: solution.token
- errorId > 0 or 30s timeout -> MANUAL FALLBACK.

STEP 3 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{{{ el.value = token; el.style.display = 'block'; }}}});
  if (window.___grecaptcha_cfg) {{{{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{{{
      const walk = (obj, d) => {{{{
        if (d > 4 || !obj) return;
        for (const k in obj) {{{{
          if (typeof obj[k] === 'function' && k.length < 3) try {{{{ obj[k](token); }}}} catch(e) {{{{}}}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}}}
      }}}};
      walk(clients[key], 0);
    }}}}
  }}}}
  return 'injected';
}}}}

For hCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{{{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For Turnstile:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{{{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For FunCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{{{ window.ArkoseEnforcement.setConfig({{{{data: {{{{blob: token}}}}}}}}) }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
You should ONLY be here if CapSolver createTask returned errorId > 0. If you haven't tried CapSolver yet, GO BACK and try it first.
If CapSolver genuinely failed (errorId > 0):
1. Audio challenge: Look for "audio" or "accessibility" button -> click it for an easier challenge.
2. Text/logic puzzles: Solve them yourself. Think step by step. Common tricks: "All but 9 die" = 9 left. "3 sisters and 4 brothers, how many siblings?" = 7.
3. Simple text captchas ("What is 3+7?", "Type the word") -> solve them.
4. All else fails -> Output RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        # Fall back to the default resume PDF when no tailored resume exists
        default_pdf = config.RESUME_PDF_PATH
        if default_pdf.exists():
            resume_path = str(default_pdf)
        else:
            # Last resort: look for the JobBot resume
            fallback = Path(os.path.expanduser("~/Code/JobBot_Zip/JoshWard_Resume.pdf"))
            if fallback.exists():
                resume_path = str(fallback)
            else:
                raise ValueError(f"No resume found for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    # Cover letter fallback text
    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # SSO domains the agent cannot sign into (loaded from config/sites.yaml)
    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    # Preferred display name
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows. You are applying for FULL-TIME salaried positions only.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

== DIALOG HANDLING ==
CRITICAL: Do NOT kill, restart, or spawn any playwright-mcp server processes. The MCP server is managed by the launcher. If MCP tools seem unreachable, they will auto-recover — just retry the tool call. Never run "kill" on playwright-mcp processes.

If a browser tool call fails because of a dialog ("Leave site?", alert, confirm, prompt):
1. Try mcp_playwright_browser_handle_dialog with accept=false
2. If that doesn't work — kill and restart Chrome via terminal:
   Run: pkill -f "remote-debugging-port=9515"
   Wait 2 seconds
   Then run: bash start-chrome.sh
   Wait 5 seconds
3. Retry your navigation. The fresh Chrome has no dialogs.

{location_check}

{salary_section}

{screening_section}

== STEP-BY-STEP ==
🚨 EARLY BAIL — check these immediately after navigation:
- Page shows Cloudflare/WAF/security block page ("Checking your browser", "Just a moment", "Request Blocked", etc.) → RESULT:FAILED:site_blocked. Do NOT retry, do NOT try cache, proxies, or curl. One block = done.
- Page returns HTTP 403, 404, 500, or any server error → RESULT:FAILED:page_error. The page is broken.
- Page requires sign-in to see the job (LinkedIn, etc.) → RESULT:FAILED:login_issue. Do NOT try to sign up or sign in.
- Page says "job closed", "no longer accepting", "position filled", "expired" → RESULT:EXPIRED.
- Page redirects to Google "sorry" CAPTCHA or any search-engine-block page → RESULT:FAILED:site_blocked.
- A browser dialog/popup appears ("Leave site?", "Confirm", "Changes you made may not be saved") → use mcp_playwright_browser_handle_dialog with accept=false to dismiss it immediately. Never let dialogs block you.

0. **ROLE CHECK** — Read the job title from the == JOB == section above. This candidate is a **Software Developer / Computer Engineer**. Apply only if the title (or job description) matches:
   - ✅ **Software** roles: Software Engineer, Software Developer, Backend, Frontend, Full Stack, Fullstack, Application Engineer, Systems Software, Embedded Software, Firmware, DevOps, SRE, Platform Engineer, Infrastructure Engineer, Cloud Engineer, Automation Engineer, Data Engineer, Data Scientist, ML Engineer, AI Engineer, Research Engineer, Algorithms Engineer
   - ✅ **Computer/Hardware Engineering** roles: Computer Engineer, Hardware Engineer, PCB Designer, FPGA Engineer, ASIC Engineer, VLSI Engineer, Systems Engineer, Electrical Engineer, Embedded Systems, Network Engineer, Security Engineer
   - ✅ Any title containing "Engineer", "Developer", "Architect", "Scientist" (in a technical/software/compute context)
   - ❌ **SKIP** (output RESULT:FAILED:not_eligible_role — this is NOT a software/hardware engineering role): Sales, Account Executive, Account Manager, Customer Success, Facilities, Coordinator, Specialist, Representative, VP, Vice President, Head of, Marketing, Recruiter, HR, Finance, Operations, Compliance, Legal, Privacy, Procurement, Supply Chain, Quality, Regulatory, Audit, Consultant, Officer, Director of (non-technical)
   
   If the role doesn't fit, output RESULT:FAILED:not_eligible_role immediately. Do NOT navigate to the page. Do NOT waste time filling forms for jobs outside this candidate's profession.

1. Use mcp_playwright_browser_navigate to go to the job URL.
2. Use mcp_playwright_browser_snapshot to read the page. Check for CAPTCHAs (see CAPTCHA section).
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button using mcp_playwright_browser_click. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: mcp_playwright_browser_snapshot. Check for CAPTCHAs.
5. Login wall?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in.
   5b. Check for popups/new windows. Use mcp_playwright_browser_snapshot to see what appeared. If it's SSO -> RESULT:FAILED:sso_required.
   5c. **Workday / create-account flow**: Read the page with mcp_playwright_browser_snapshot. Determine which form is showing:

      **CREDENTIALS MANAGEMENT:**
      Before attempting sign-in or create-account, run this terminal command to check if
      credentials already exist for this site:
        python3 ~/.applypilot/credentials_manager.py get {job.get('site', 'unknown')}
      If credentials exist, use those credentials instead of the profile default.
      If credentials exist and login fails, try password reset (see 5h below).

      DECISION TREE (follow strictly, no looping):
      
      A) **Sign In form** (has "Sign In" heading, email + password fields, "Sign In" button):
         - If you have NOT yet created an account: click "Create Account" / "Don't have an account?" link -> go to B
         - If you HAVE created an account earlier in this session but haven't verified email: go to step 5e first
         - Otherwise: try signing in with email and password from saved credentials or profile
      
      B) **Create Account / Register form** (has "Create Account" heading, email + password fields):
         Fill in: email = {personal['email']}, password = "{personal.get('password', '')}", 
         check the consent/checkbox, click Submit/Create Account.
         Do NOT look for a verifyPassword field — if it's not on the page, skip it.
         AFTER SUBMITTING: go directly to step 5e (email verification). Do NOT try to sign in yet.
      
      C) **Already have an account? Sign In** link visible on create-account page:
         Click it, then follow A.
      
      D) **Don't have an account yet? Create Account** link visible on sign-in page:
         Click it, then follow B.
   5d. After clicking Login/Sign In/Create Account: wait for navigation, then mcp_playwright_browser_snapshot. Check for CAPTCHAs.
   5e. **Email verification** (CRITICAL — do NOT skip this):
       After creating an account, Workday and many ATS send a verification email.
       If the page shows "Check your email", "Verify your email", "Confirmation sent", or similar:
       1. Wait 10 seconds for the email to arrive
       2. Run via terminal: python3 ~/.applypilot/email_verifier.py search "subject:(verify OR confirm OR welcome OR activate)"
       3. If results found, get the link:
          python3 ~/.applypilot/email_verifier.py extract-link <msg_id>
       4. Use mcp_playwright_browser_navigate to go to that confirmation link
       5. Wait for the "email confirmed" / "account verified" page
       6. Navigate back to the original job URL and sign in with the credentials you just created
       If no verification email appears and the page just shows the job again, you may already be signed in.
       Try clicking Apply again. If the form/application page loads, skip to step 6.
   5f. **Save credentials after successful account creation**:
       After successfully creating an account (email verified, signed in, or past the login wall),
       run this terminal command to save the credentials for future use:
         python3 ~/.applypilot/credentials_manager.py save {job.get('site', 'unknown')} {personal['email']} "{personal.get('password', '')}"
       This ensures the next time the pipeline encounters this site, it can sign in directly
       without creating another account.
   5g. Sign in succeeded? Continue to step 6. Sign in failed or page didn't change?
       - First attempt: try the other flow (Create Account if you tried Sign In, or vice versa)
       - Second attempt: try password recovery (step 5h)
       - Never try the same approach more than twice
   5h. **Password recovery** (try once, then move on):
       If sign-in and create-account both failed:
       1. Look for a "Forgot password?" / "Reset password" link -> click it
       2. Enter the email: {personal['email']}
       3. Wait 15 seconds for the reset email to arrive
       4. Check email for reset links:
          python3 ~/.applypilot/email_verifier.py search "subject:(reset OR password OR forgot)"
       5. If found, extract and navigate to the link, set new password
       6. Save updated credentials:
          python3 ~/.applypilot/credentials_manager.py update-password {job.get('site', 'unknown')} "<new_password>"
       7. Navigate back to the job URL and sign in with the new password
   5i. If sign-in still fails after password recovery: move on. The site requires SSO or has a broken login flow.
       Output RESULT:FAILED:login_issue. Do NOT loop back to create-account — it will fail the same way.
6. Upload resume. Use mcp_playwright_browser_file_upload to set the resume file.
   File path: {pdf_path}
   The file input selector from snapshot has a target like "e123". Use mcp_playwright_browser_file_upload with paths=["{pdf_path}"].
7. Upload cover letter if there's a field for it. Use mcp_playwright_browser_type for text, mcp_playwright_browser_file_upload for file upload.
8. Fill the form using mcp_playwright_browser_type for text fields, mcp_playwright_browser_click for checkboxes/buttons/links, mcp_playwright_browser_select_option for dropdowns. For 3+ fields, use mcp_playwright_browser_fill_form with a fields array.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After submit: mcp_playwright_browser_snapshot. Check for CAPTCHAs. Look for "thank you" or "application received".
12. Output your result.

== RESULT CODES (output EXACTLY one) ==
🚨 CRITICAL: Your very last line MUST be exactly one RESULT:... code. The system scans for this to determine the outcome. If you don't output it, the job is marked as failed with no_result_line.
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:not_eligible_role -- this is NOT a software/hardware engineering role (Sales, Marketing, Finance, etc.)
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- All MCP Playwright tools use target="ref" parameter format (e.g. target="e47"). Do NOT use ref="e47" — that's a different convention. The target value is the ref ID string (without @).
- Use mcp_playwright_browser_snapshot ONCE per page to understand it. Then use it again when you need current element refs for clicking/filling.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields you can before making the next API call. Batch your work.
- Use mcp_playwright_browser_fill_form with a fields array when you have 3+ fields to fill at once — it's much faster than one-at-a-time.
- Keep your thinking SHORT. Don't repeat page structure back. Just state what you're doing next.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- check for CAPTCHAs. Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently.

== FORM TRICKS ==
- Popup/new window opened? Use mcp_playwright_browser_snapshot to see what appeared. Check the URL.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Use mcp_playwright_browser_file_upload to set the file, wait for parsing to finish, then click Next/Continue.
- Dropdown won't fill? mcp_playwright_browser_click to open it, then mcp_playwright_browser_click the option, or use mcp_playwright_browser_select_option.
- Checkbox won't check? Use mcp_playwright_browser_click on it. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take mcp_playwright_browser_snapshot to see error messages. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.
- **Stuck on a specific field?** Give your best answer and move on. Do NOT restart the job, do NOT go back to the job page, do NOT navigate away. One stuck field is better than zero fields + a lost application. If you absolutely cannot interact with a field, leave it empty (skip) and continue - the next button will show validation errors if required. Use mcp_playwright_browser_fill_form for 3+ fields to batch them.

== FILE UPLOAD ==
Use mcp_playwright_browser_file_upload to upload the resume. This calls the built-in Playwright file uploader directly — it works with the browser's file input.

1. Find the file input element using mcp_playwright_browser_snapshot (look for 'input[type=file]' or upload buttons)
2. Run: mcp_playwright_browser_file_upload with paths=["{pdf_path}"] 
3. The file will be set on the form. Snapshot to confirm it was accepted.
4. If the file input is hidden (common in Workday, Lever): click the upload area first to make the input active, then use mcp_playwright_browser_file_upload.
5. Still failing after 2 tries? Skip upload and continue filling the rest of the form.

{captcha_section}

== TIME LIMIT ==
You have until the process timeout (~15 min) to complete this application. Work efficiently but don't rush — there is NO iteration cap. Every job is winnable.
- Log a brief status after each iteration (what you did, what you found). This helps diagnose where applications get stuck.
- If a specific field or upload isn't working after several attempts, skip it and move to the next step. Don't let one problem block the entire application.
- Persistence wins: try different approaches to get past a stuck page. Try clicking different buttons, scrolling, filling fields in a different order. The form WILL cooperate eventually.

== LOGGING-FIRST ==
- Add a log line at the START of every job attempt so you can always tell when a job began vs. when it failed.
- After every significant action (navigation, form fill, click, error), add a brief log entry.
- When something goes wrong, check your logs before guessing at the problem. The logs will show you exactly how far you got and where it broke.

== WHEN TO GIVE UP ==
Only give up when the page itself confirms the job is unreachable:
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
- CAPTCHA repeatedly fails (after CapSolver + manual fallback) -> RESULT:CAPTCHA
- Stuck on the same page with zero progress indicators after many different approaches -> RESULT:FAILED:stuck (only after trying 5+ different strategies)
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
