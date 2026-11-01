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
    personal = profile.get('personal', {})
    auth = profile.get('work_authorization', {})
    comp = profile.get('compensation', {})
    exp = profile.get('experience', {})
    avail = profile.get('availability', {})
    eeo = profile.get('eeo_voluntary', {})
    lines = [f"Name: {personal.get('full_name', '')}", f"Email: {personal.get('email', '')}", f"Phone: {personal.get('phone', '')}"]
    address = ', '.join(str(x) for x in (personal.get('address',''), personal.get('city',''), personal.get('province_state',''), personal.get('country',''), personal.get('postal_code','')) if x)
    if address: lines.append(f'Address: {address}')
    for key, label in (('linkedin_url','LinkedIn'),('github_url','GitHub'),('portfolio_url','Portfolio'),('website_url','Website')):
        if personal.get(key): lines.append(f"{label}: {personal[key]}")
    lines.append(f"Legally authorised to work in India: {auth.get('legally_authorized_to_work', 'Not specified')}")
    lines.append(f"Employer sponsorship required in India: {auth.get('require_sponsorship', 'Not specified')}")
    if comp.get('salary_expectation'): lines.append(f"Expected salary: ₹{int(comp['salary_expectation']) / 100000:g} LPA per annum")
    if comp.get('salary_range_min') and comp.get('salary_range_max'):
        lines.append(f"Acceptable range: ₹{int(comp['salary_range_min']) / 100000:g}–₹{int(comp['salary_range_max']) / 100000:g} LPA")
    if exp.get('years_of_experience_total'): lines.append(f"Years of experience: {exp['years_of_experience_total']}")
    if exp.get('education_level'): lines.append(f"Education: {exp['education_level']}")
    if avail.get('earliest_start_date'): lines.append(f"Earliest availability: {avail['earliest_start_date']}")
    lines.extend(['Age 18+: Yes','Willing to undergo a background check: Yes','Felony history: No (as confirmed by applicant)','Previously worked for this employer: Yes only for Microsoft or UIDAI; otherwise No','How heard: Online Job Board'])
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race/ethnicity: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran status: {eeo.get('veteran_status', 'Decline to self-identify')}")
    lines.append(f"Disability status: {eeo.get('disability_status', 'Decline to self-identify')}")
    return '\n'.join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    personal = profile.get('personal', {})
    cfg = search_config.get('location', {})
    patterns = cfg.get('accept_patterns', [])
    city_list = ', '.join(patterns) if patterns else personal.get('city', 'Hyderabad')
    return f"""== LOCATION CHECK (before any form) ==
Candidate is based in India and can relocate within India. Preferred onsite/hybrid locations: {city_list}.
- Fully remote is eligible only if the employer explicitly permits work from India or says worldwide/work from anywhere.
- Remote roles restricted to another country are NOT eligible.
- Hybrid/onsite in a listed preferred Indian location is eligible.
- Hybrid/onsite elsewhere in India: stop for manual review; do not assume.
- Hybrid/onsite outside India: NOT ELIGIBLE.
- If remote country eligibility or location is unclear, stop for manual review.
For ineligible roles output RESULT:FAILED:not_eligible_location.
For unclear roles output RESULT:NEEDS_REVIEW:location_unclear.
Do not fill or submit an application until eligibility is established."""


def _build_salary_section(profile: dict) -> str:
    comp = profile.get('compensation', {})
    currency = comp.get('salary_currency', 'INR')
    expected = comp.get('salary_expectation', '1500000')
    minimum = comp.get('salary_range_min', '1400000')
    maximum = comp.get('salary_range_max', '8000000')
    if currency == 'INR':
        e, lo, hi = (int(float(v)) / 100000 for v in (expected, minimum, maximum))
        return f"""== SALARY ==
Expected target: ₹{e:g} LPA per annum.
Minimum acceptable: ₹{lo:g} LPA. Never state willingness below this.
Maximum target: ₹{hi:g} LPA.
For an Indian role with an INR salary range, use its midpoint, bounded by the ₹{lo:g}–₹{hi:g} LPA range.
If no salary is posted, use ₹{e:g} LPA. If asked for a range and no employer range is shown, use ₹{lo:g}–₹{hi:g} LPA.
Confirm whether pay is monthly or annual. Do not use US-specific seniority salary rules.
If a form requires another currency, do not guess an exchange rate; stop for manual review."""
    return f"""== SALARY ==
Expected: {expected} {currency} per annum; minimum: {minimum}; maximum: {maximum}.
Use the stated pay period. If conversion is needed and no current verified exchange rate is available, stop for manual review."""


def _build_screening_section(profile: dict) -> str:
    personal = profile.get('personal', {})
    exp = profile.get('experience', {})
    auth = profile.get('work_authorization', {})
    return f"""== SCREENING QUESTIONS ==
Candidate is based in {personal.get('city', 'Hyderabad')}, India; willing to relocate within India, not outside India.
Work authorisation in India: {auth.get('legally_authorized_to_work', 'Not specified')}; sponsorship required in India: {auth.get('require_sponsorship', 'Not specified')}.
Previously worked for this employer: YES for Microsoft and UIDAI; NO for other employers.
Willing to undergo a background check: YES. If asked whether a past check failed or about adverse findings, do not infer.
Felony history: NO, as confirmed by applicant.
Citizenship, visa status, security clearance, licences and other legal facts: use explicit profile facts only.
Target roles: {exp.get('target_role', 'entry-level product or engineering')}; stated experience: {exp.get('years_of_experience_total', '1')} year(s).
Answer YES to skills questions only when supported by actual resume experience; do not claim adjacent tools as hands-on experience.
Write concise, specific open-ended answers grounded in the job description and real resume achievements.
For EEO questions, follow the applicant's preference to decline to self-identify."""


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
    return """== CAPTCHA / ANTI-AUTOMATION ==
If a CAPTCHA, bot challenge, identity challenge, or anti-automation check appears:
1. Stop all browser interaction immediately. Do not click Submit or retry the blocked action.
2. Do not solve, outsource, bypass, defeat, inject tokens, or use CAPTCHA-solving services.
3. Output exactly RESULT:CAPTCHA and stop. The user will handle the challenge manually outside this automation.
4. Do not claim the application was submitted. Do not retry this job automatically.
Never submit while a verification challenge remains unresolved."""


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
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")
    if src_pdf.stat().st_size <= 1000 or src_pdf.read_bytes()[:5] != b"%PDF-":
        raise ValueError(f"Tailored resume is not a valid non-empty PDF: {src_pdf}")

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
        submit_instruction = "DRY RUN: Do NOT click Submit, Apply, send an application email, or perform any final submission action. Review the form and fields only, then output exactly RESULT:DRY_RUN. Never output RESULT:APPLIED for a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an assistant helping prepare and, when explicitly permitted by this run, submit a truthful job application. Follow every safety and eligibility rule below. Never bypass security controls, invent facts, or treat an uncertain outcome as success.

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

If something unexpected happens and these instructions do not cover it, stop and output RESULT:NEEDS_REVIEW. Do not guess, bypass controls, or take irreversible actions outside the explicit steps below.

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

{location_check}

{salary_section}

{screening_section}

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. If any CAPTCHA, bot challenge, identity challenge, or anti-automation check is present or suspected, stop immediately and output RESULT:CAPTCHA. Never solve or bypass it.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED only if the email tool explicitly confirms successful sending. If sending fails or the result is unclear, output RESULT:NEEDS_REVIEW. Never claim an email application was sent without tool confirmation.
   After clicking Apply: browser_snapshot. If a CAPTCHA or anti-automation challenge appears, stop and output RESULT:CAPTCHA. Do not solve it or continue.
5. Login wall?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Try sign in: {personal['email']} / {personal.get('password', '')}
   5d. After clicking Login/Sign-in: if a CAPTCHA or anti-automation challenge appears, stop and output RESULT:CAPTCHA. Do not solve it or retry login.
   5e. Sign in failed? Try sign up with same email and password.
   5f. Need email verification? Use search_emails + read_email to get the code.
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. All failed? Output RESULT:FAILED:login_issue. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After clicking Submit: take a browser_snapshot. If a CAPTCHA/challenge appears, do not interact further; output RESULT:SUBMISSION_UNVERIFIED because the submission outcome is uncertain. Do not click Submit again. Otherwise verify a clear on-page success message such as "application received", "thank you", or an application confirmation/reference. Only then output RESULT:APPLIED. If the click occurred but no reliable success state is visible, output RESULT:SUBMISSION_UNVERIFIED. A confirmation email is not required when the portal itself clearly confirms receipt.
12. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- the portal clearly confirms that the application was received
RESULT:DRY_RUN -- preview only; no submission action was taken
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- stopped at a CAPTCHA or anti-automation challenge; user action required
RESULT:NEEDS_REVIEW -- eligibility, form content, or site behavior is unclear; no submission made
RESULT:SUBMISSION_UNVERIFIED -- Submit was clicked but no reliable success state could be verified; never retry automatically
RESULT:SUBMITTED_PENDING_CONFIRMATION -- portal explicitly says submitted/received but shows a separate pending confirmation state; never retry automatically
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: If a visible or suspected CAPTCHA/anti-automation challenge appears, stop and use the appropriate CAPTCHA or SUBMISSION_UNVERIFIED result code. Do not run scripts to defeat, bypass, or solve challenges.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take a snapshot and screenshot. If the site clearly indicates the application was not submitted, correct ordinary form errors only when safe. Never retry if the submission outcome is ambiguous.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
