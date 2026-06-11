"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells the AI agent
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
    """Format the applicant profile section of the prompt."""
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
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")
    for url_field in ("linkedin_url", "github_url", "portfolio_url", "website_url"):
        if personal.get(url_field):
            label = url_field.replace("_url", "").replace("_", " ").title()
            lines.append(f"{label}: {personal[url_field]}")

    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Expectation: ${comp['salary_expectation']} {currency}")
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")
    lines.extend([
        "Age 18+: Yes", "Background Check: Yes", "Felony: No",
        "Previously Worked Here: No", "How Heard: Online Job Board",
    ])
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")
    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section."""
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))
    city_list = ", ".join(accept_patterns) if accept_patterns else primary_city
    return f"""== LOCATION CHECK ==
REMOTE jobs -> Always APPLY regardless of candidate location.
ONSITE/HYBRID jobs whose work location is in: {city_list} -> APPLY.
ONSITE/HYBRID jobs NOT in the above list + with "remote OK" in description -> APPLY.
ONSITE/HYBRID jobs NOT in the above list and no "remote OK" -> RESULT:FAILED:not_eligible_location.
Overseas jobs + no remote -> SAME.
If unknown -> proceed, answer screening honestly.

IMPORTANT: This check is about the JOB's work location, not the candidate's
home city.  A remote job is always eligible regardless of where you live."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions."""
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if str(floor).isdigit() else floor)
    return f"""== SALARY ==
${floor} {currency} floor. Posted range -> midpoint. Senior/Staff/Lead -> min $110K {currency}.
No info -> ${floor}. Asked range -> midpoint ±10% or "${range_min}-${range_max} {currency}".
Hourly -> annual ÷ 2080."""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))

    return f"""== SCREENING QUESTIONS ==
Hard facts -> truthful (location: {city}, no relocation; work auth: see profile).
Skills -> confident. {target_role} with {years}yr. Same-domain tools -> YES.
Open-ended -> 2-3 sentences specific to this job. Reference resume. No fluff.
EEO -> always decline / prefer not to say.
Sponsorship -> No. Work Auth -> US Citizen. Previously employed -> No. Felony -> No.
Source -> Online Job Board or LinkedIn. Salary -> use SALARY section.
Unfamiliar dropdown -> closest match, move on."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]
    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name
    permit_type = work_auth.get("work_permit_type", "")
    sponsorship = work_auth.get("require_sponsorship", "")
    auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}." if permit_type else "Work auth: Answer truthfully."
    name_rule = f'Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless field says "legal name".'
    return f"""== HARD RULES ==
1. Never lie about: citizenship, work auth, criminal history, education, clearance, licenses.
2. {auth_rule}
3. {name_rule}
4. VERIFY BEFORE CLAIMING SUCCESS: Never output RESULT:APPLIED unless you
   personally clicked Submit AND saw a confirmation page with your own
   screenshot.  Do NOT trust your own prior reasoning — the confirmation
   screenshot is the only proof that matters.  If you didn't take a
   post-submit screenshot showing a confirmation message, you did not
   apply yet.  Keep trying."""


def _build_captcha_section() -> str:
    """Build compact CAPTCHA instructions."""
    config.load_env()
    capsolver_key = os.environ.get("CAPSOLVER_API_KEY", "")
    if capsolver_key:
        return f"""== CAPTCHA ==
CapSolver key set. API: https://api.capsolver.com
Detect: browser_evaluate JS to find hcaptcha/recaptcha/turnstile sitekey.
Solve: POST createTask -> poll getTaskResult -> inject token via browser_evaluate.
If CapSolver fails -> MANUAL FALLBACK: try audio challenge, solve simple puzzles, then RESULT:CAPTCHA."""
    return """== CAPTCHA ==
CapSolver NOT configured.
DO NOT output RESULT:CAPTCHA unless you have navigated to the job page
and can SEE a CAPTCHA challenge (hCaptcha, reCAPTCHA, Turnstile, etc.)
on the page. If you haven't reached the page yet, keep going.
If you see a CAPTCHA on the page, try clicking audio challenge or
solving simple puzzles. Truly stuck -> RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    """Build the full instruction prompt for the apply agent.

    Args:
        job: Job dict from the database.
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, don't click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # Resolve resume PDF path — always use the honest default resume
    default_pdf = config.RESUME_PDF_PATH
    if default_pdf.exists():
        resume_path = str(default_pdf)
    else:
        fallback = Path(os.path.expanduser("~/Code/JobBot_Zip/JoshWard_Resume.pdf"))
        if fallback.exists():
            resume_path = str(fallback)
        else:
            raise ValueError(f"No resume found at {config.RESUME_PDF_PATH}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # Cover letter handling
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # Build prompt sections
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())
    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    if dry_run:
        submit_instruction = "Do NOT click Submit. Review, then output RESULT:APPLIED (dry run)."
    else:
        submit_instruction = "Before Submit, snapshot + verify ALL fields. Fix errors first."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF: {pdf_path}
Cover Letter PDF: {cl_upload_path or "N/A"}

== RESUME TEXT ==
{tailored_resume}

== COVER LETTER TEXT ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application using browser tools. Navigate, read, click, fill. Goal: submission.

{hard_rules}

== ANTI-BOT FIELDS ==
Some forms include a hidden honeypot field with label "Enter website" or "for robots only"
or "do not enter if you're human." This is an anti-bot TRAP. NEVER fill these fields.
Leave them completely empty. If you see one, IGNORE IT — don't even mention it.

== NEVER DO (RESULT:FAILED) ==
Freelancing/contract platforms (Upwork, Fiverr, etc.) — not a real job app -> RESULT:FAILED:not_a_job_application.
Camera/mic/screen/location permissions, video/selfie/biometric, browser extensions, payment/SSN — cannot fulfill -> RESULT:FAILED:unsupported_requirement.

== DIALOGS ==
Never kill MCP. handle_dialog(accept=false). If fails: Chrome should auto-restart (start-chrome.sh wrapper handles this).

{location_check}

{salary_section}

{screening_section}

== CREDENTIAL TOOLS (Use Instead of CLI Scripts) ==
- credentials_get(site="companyname")  — check for saved login
- credentials_save(site, email, password)  — save new credentials
- credentials_list()  — see all saved sites
- credentials_update_password(site, new_password)  — after reset
- email_search(query)  — find verification/password-reset emails
- email_read(msg_id)  — read full email body
- email_extract_link(msg_id)  — get URL from verification/reset email

== STEPS ==
EARLY BAIL (if any match, output RESULT and STOP immediately):
  Cloudflare/WAF -> site_blocked | 403/404/500 -> page_error | "no longer accepting" -> EXPIRED.
  Navigated to a URL but the page doesn't show this job (redirected to careers homepage, wrong listing)?
  -> page_error. The link is dead.

0. ROLE CHECK — Read the job title from the == JOB == section at the top.
   If it is NOT software/engineering/developer/architect/scientist/AI/ML ->
   output RESULT:FAILED:not_eligible_role. STOP. Do NOT navigate to the URL.

1. LOCATION CHECK — Read the location from the == JOB == section at the top.
   This is the JOB's work location. Compare it against the rules in the
   == LOCATION CHECK == section.  Do NOT use your own home city — use the
   job's listed location.  If the job is remote, skip this check.
   If location is not eligible -> output RESULT:FAILED:not_eligible_location. STOP.

2. Navigate to the job URL. Snapshot the page. Look for the Apply button.

3. Click the Apply button. If email-to-apply specified -> send email -> APPLIED.

4. LOGIN WALL? After clicking Apply, check the page.
   - If the ONLY options are "Sign in with Google/Apple/LinkedIn" with no
     email/password option -> RESULT:LOGIN_ISSUE. STOP Immediately.
   - If email/password form exists -> FOLLOW THESE STEPS IN ORDER:
     1) Call credentials_get(site="companyname"). If saved creds exist, use them.
     2) Click "Sign in with email" if needed, fill fields, submit.
     3) If sign-in fails -> Try forgot-password ONCE. If reset email arrives ->
        extract link -> navigate -> set new password -> sign in again.
        If no reset email arrives, or reset doesn't help -> RESULT:LOGIN_ISSUE. STOP.
        Do NOT loop forgot-password more than once per session — account will lock.
     4) Once signed in, continue to step 5.
   - If no login wall at all -> continue to step 5.

5. Upload resume: mcp_playwright_browser_file_upload(paths=["{pdf_path}"])
   If the native dialog gets stuck -> press Escape to dismiss it.
   If file_upload fails, use mcp_playwright_browser_run_code_unsafe to upload via
   Playwright JS: upload the file programmatically through the page.
   Retry until the resume is uploaded — this is mandatory.

6. Upload cover letter if applicable.

7. Fill form fields. Use fill_form for 3+ fields at once.

8. Answer screening questions truthfully using profile data above.

9. {submit_instruction}

10. After submit: IMMEDIATELY take a full-page screenshot. Read the visible text
    on the page. You MUST see a confirmation message ("thank you", "application
    received", "submitted successfully") in the page text before proceeding.
    If you don't see a confirmation, you did NOT successfully submit — retry
    or RESULT:FAILED:reason.

11. Output RESULT code (see below). Never output RESULT:APPLIED unless you
    personally clicked Submit and then confirmed the submission via screenshot.
    If APPLIED, the NEXT line must be the full confirmation page URL you saw
    after submitting.  No URL = no proof = your session will be rejected.

    CORRECT:
    RESULT:APPLIED
    https://company.wd5.myworkdayjobs.com/confirmation/ABC123

    WRONG (will be rejected):
    RESULT:APPLIED
    (no URL follows)

== RESULT CODES ==
APPLIED | EXPIRED | CAPTCHA | LOGIN_ISSUE
FAILED:not_eligible_location | FAILED:not_eligible_work_auth
FAILED:not_eligible_role | FAILED:unsupported_requirement | FAILED:reason

== TIPS ==
- Use target="ref" format (e.g. target="e47")
- Multi-page forms: snapshot each page, fill all, click Next/Continue
- One snapshot per page. Another when refs expire.
- Popup? Snapshot it.
- Dropdown: click to open, then click option.
- Phone: {phone_digits} | Date: {datetime.now().strftime('%m/%d/%Y')}
- Calendar/datepicker? Don't click through months. Click the date field and TYPE the date directly (MM/DD/YYYY).
- Already applied? If the page shows "already applied", "in process", "submitted" for this job -> RESULT:APPLIED immediately.
- Validation errors? Snapshot messages, fix all, retry.
- Stuck on field? Give best answer, move on.
- TOOL MODAL STATE: If a tool returns an error like "Tool X does not handle the modal state"
  with "can be handled by Y", call tool Y IMMEDIATELY. Do NOT retry tool X — it will
  keep failing. The error message tells you exactly which tool to use.
- Workday dropdown not clicking? First try fill_form(type="combobox") in one shot. If that fails, use browser_run_code_unsafe to set the value via JS directly (instant, one call). Last resort: type into searchable dropdowns.
- File upload hidden? Click area first. If dialog gets stuck: Escape to dismiss, then retry.

{captcha_section}

== GIVE UP ==
Closed/expired -> EXPIRED. Page broken -> page_error. CAPTCHA unsolvable -> CAPTCHA.
If you've exhausted all approaches, output a RESULT code explaining why."""
    return prompt
