"""Auto-fill and submit the IISM Gulmarg admission form.

Waits for seats to open for the chosen batch, then fills the form and submits once.
If a submit fails (any reason, including the site's mobile-number error) it keeps
trying until one succeeds or you press Ctrl+C.

  .venv/bin/python apply.py --check       # validate config + compress documents only
  .venv/bin/python apply.py --dry-run     # fill everything, don't click Submit
  .venv/bin/python apply.py --test        # same, without waiting for seats (before batches open)
  .venv/bin/python apply.py               # wait for seats, fill, submit
"""
import argparse
import itertools
import json
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
URL = "https://admission.iismgulmarg.in/admission_form.aspx?mobile={mobile}"
MAX_BYTES = 1_048_576          # site rejects files > 1 MB
TARGET_BYTES = 450_000         # page text says signatures/photo must be <= 500 KB
IMG = {".jpg", ".jpeg", ".png", ".gif"}
IMG_PDF = IMG | {".pdf"}

# config key -> (input id, required, allowed extensions)
# Birth certificate's validator regex is buggy ([Pp][Df][Ff]) and rejects ".pdf", so images only.
FILE_FIELDS = {
    "birth_certificate": ("FupBirthCertificate", True, IMG),
    "education": ("FupEducationQualification", True, IMG_PDF),
    "photo": ("FupPhototOfApplicant", True, IMG),
    "signature": ("FupSignatureOfAppicant", True, IMG),
    "guardian_signature": ("FupParentGuardianSignature", True, IMG),
    "domicile_proof": ("FupDomicileAttachProof", False, IMG_PDF),
    "adventure_course": ("FupAnyAdventureCoursePhoto", False, IMG_PDF),
    "employment": ("FupCurrentEmployment", False, IMG_PDF),
}

TEXT_FIELDS = {
    "name": "txtName", "father_name": "txtFatherName", "nationality": "txtNationality",
    "domicile": "txtDomicile", "house_no": "txtHouseNoApartment", "street": "txtStreetVillageColony",
    "district_city": "txtDistrictCity", "pincode": "txtPincode", "mobile": "txtMobileNo",
    "email": "txtEmailId", "blood_group": "txtBloodGroup", "medical_category": "txtMedicalCategory",
    "height": "txtHeight", "weight": "txtWeight", "waist": "txtWaist", "shoe_size": "txtShoeSize",
    "emergency_name": "txtEmergencyContactName", "emergency_phone": "txtEmergencyContactPhone",
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def compress_image(src: Path, out_dir: Path) -> Path:
    """Re-encode as JPEG with macOS `sips`, shrinking until under TARGET_BYTES."""
    dst = out_dir / (src.stem + ".jpg")
    for max_px, quality in [(2000, 85), (1600, 80), (1400, 70), (1200, 60), (1000, 50), (800, 40)]:
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
                        "-Z", str(max_px), str(src), "--out", str(dst)],
                       check=True, capture_output=True)
        if dst.stat().st_size <= TARGET_BYTES:
            return dst
    raise SystemExit(f"Could not compress {src} below 1 MB")


def prepare_files(cfg) -> dict:
    """Validate every document and return {input_id: path ready to upload}."""
    out_dir = ROOT / "prepared"
    out_dir.mkdir(exist_ok=True)
    ready, problems = {}, []
    for key, (input_id, required, allowed) in FILE_FIELDS.items():
        raw = cfg["files"].get(key, "")
        if not raw:
            if required:
                problems.append(f"{key}: required but not set")
            continue
        p = (ROOT / raw).resolve()
        if not p.exists():
            problems.append(f"{key}: file not found: {p}")
            continue
        ext = p.suffix.lower()
        if ext not in allowed:
            if ext in IMG | {".heic", ".webp"} and allowed & IMG:
                p = compress_image(p, out_dir)  # convert to JPG
            else:
                problems.append(f"{key}: {ext} not allowed (allowed: {sorted(allowed)})")
                continue
        if p.stat().st_size > TARGET_BYTES:
            if p.suffix.lower() == ".pdf":
                problems.append(f"{key}: PDF is {p.stat().st_size // 1024} KB (> 1 MB); send a JPG scan instead")
                continue
            p = compress_image(p, out_dir)
        log(f"  {key:20s} {p.name} ({p.stat().st_size // 1024} KB)")
        ready[input_id] = str(p)
    missing = [k for k, v in cfg.items() if isinstance(v, str) and v.startswith("FILL_ME")]
    if missing:
        problems.append(f"fill these in config.json: {', '.join(missing)}")
    if problems:
        raise SystemExit("Config problems:\n  " + "\n  ".join(problems))
    return ready


def pick(page, select_id, wanted):
    """Select the option whose text contains `wanted` (case-insensitive); waits for the postback."""
    options = page.locator(f"#{select_id} option").all_inner_texts()
    match = next((o for o in options if wanted.lower() in o.lower() and not o.lower().startswith("select")), None)
    if not match:
        return None, options
    current = page.locator(f"#{select_id} option:checked").inner_text()
    if current == match:  # already selected: selecting again fires no postback
        return match, options
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.select_option(f"#{select_id}", label=match)
    return match, options


def read_slot(page, cfg, sched):
    """Select a schedule and read its seats. Returns ("open", n), ("full", 0) or ("unclear", why).

    The seats box defaults to "0", so a failed/partial postback looks exactly like a full batch.
    A zero only counts as "full" when the page provably reflects this schedule: the dropdown
    shows it and the server filled in its age limits (0 until a schedule is loaded).
    """
    try:
        pick(page, "ddlScheduleCourse", sched)
        pick(page, "ddlState", cfg["state"])
        selected = page.locator("#ddlScheduleCourse option:checked").inner_text()
        raw = page.input_value("#txtAvailableSeat").strip()
        max_age = page.input_value("#hdfMaximumAge").strip()
    except PWTimeout:
        return "unclear", "timeout"
    except Exception as e:  # error page, missing elements, etc.
        return "unclear", type(e).__name__
    if selected != sched:
        return "unclear", f"dropdown shows '{selected[:30]}'"
    if not raw.isdigit():
        return "unclear", f"seats box '{raw}'"
    if int(raw) > 0:
        return "open", int(raw)  # a positive count can't come from the default
    if max_age in ("", "0"):
        return "unclear", "zero but schedule details not loaded"
    return "full", 0


class SlotTracker:
    """Counts consecutive verified-full reads per preference, across page reloads."""

    def __init__(self):
        self.zeros, self.since = {}, {}

    def record(self, pref, status):
        if status == "full":
            self.zeros[pref] = self.zeros.get(pref, 0) + 1
            self.since.setdefault(pref, time.time())
        elif status in ("open", "unlisted"):
            self.zeros.pop(pref, None)
            self.since.pop(pref, None)
        # "unclear" neither confirms nor resets

    def confirmed(self, pref, reads, seconds=0):
        return (self.zeros.get(pref, 0) >= reads
                and time.time() - self.since.get(pref, time.time()) >= seconds)

    def summary(self, prefs):
        return ", ".join(f"{p}: {self.zeros.get(p, 0)}x full" for p in prefs)


def wait_for_seats(page, cfg, poll, tracker, no_wait=False):
    """Reload until a preference is open AND every higher preference is confirmed full.

    cfg["schedule"]: substrings in preference order. The first `top` are the main choices:
    moving from one to the next needs `confirm_reads` consecutive verified-full reads of every
    choice above it. Moving past the top choices needs `deep_confirm_reads` reads of each,
    spread over at least `deep_confirm_seconds`. A batch that isn't listed is never "full".
    """
    url = URL.format(mobile=cfg["mobile"])
    prefs = cfg["schedule"] if isinstance(cfg["schedule"], list) else [cfg["schedule"]]
    top = cfg.get("top_choices", 3)
    reads = cfg.get("confirm_reads", 3)
    deep_reads = cfg.get("deep_confirm_reads", 6)
    deep_secs = cfg.get("deep_confirm_seconds", 60)

    def allowed(i):
        """May preference i be taken, given what we know about the ones above it?"""
        if any(not tracker.confirmed(p, reads) for p in prefs[:i]):
            return False
        return i < top or all(tracker.confirmed(p, deep_reads, deep_secs) for p in prefs[:top])

    attempt = 0
    while True:
        attempt += 1
        waiting_on_confirm = False
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            got, opts = pick(page, "ddlCourse", cfg["course"])
            if not got:
                raise LookupError(f"course not listed; options: {opts}")
            got, opts = pick(page, "ddlCourseLevel", cfg["course_level"])
            if not got:
                raise LookupError(f"level not listed yet; options: {opts}")
            schedules = [o for o in page.locator("#ddlScheduleCourse option").all_inner_texts()
                         if not o.lower().startswith("select")
                         and not any(x.lower() in o.lower() for x in cfg.get("schedule_exclude", []))]
            if not schedules and no_wait:
                log("No schedules published; continuing for test")
                pick(page, "ddlState", cfg["state"])
                return
            report = []
            for i, pref in enumerate(prefs):
                # Lower choices are only read once the top ones are close to deep-confirmed.
                if i >= top and not all(tracker.confirmed(p, reads) for p in prefs[:top]):
                    break
                sched = next((s for s in schedules if pref.lower() in s.lower()), None)
                if not sched:
                    tracker.record(pref, "unlisted")
                    report.append(f"{pref}=not listed")
                    continue
                status, info = read_slot(page, cfg, sched)
                tracker.record(pref, status)
                report.append(f"{pref}={status}({info})")
                if status == "open" or (no_wait and status != "unclear"):
                    if allowed(i) or no_wait:
                        log(f"TAKING '{sched}' ({info} seats). Checks: {'; '.join(report)}")
                        return
                    # Open, but a higher choice isn't proven full yet: recheck immediately.
                    waiting_on_confirm = True
            log(f"#{attempt} {'; '.join(report) or 'nothing readable'} | streaks: {tracker.summary(prefs[:top])}"
                + (" | lower choice open, re-confirming higher ones" if waiting_on_confirm else ""))
            if not schedules:
                log("  no batches published yet")
        except Exception as e:  # not listed yet, timeout, network drop, server error page
            log(f"#{attempt} waiting: {str(e).splitlines()[0][:200]}")
        if not waiting_on_confirm:
            time.sleep(poll + random.uniform(0, 1))


def fill_and_submit(page, cfg, files, dry_run):
    # DOB triggers a full postback, which clears file inputs, so do it before files.
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.fill("#txtDateOfBirth", cfg["dob"])
        page.locator("#txtDateOfBirth").dispatch_event("change")
    if page.input_value("#txtDateOfBirth") != cfg["dob"]:
        # Postback sometimes renders the box empty; set it directly without re-triggering onchange.
        page.eval_on_selector("#txtDateOfBirth", "(el, v) => el.value = v", cfg["dob"])
    for key, field in TEXT_FIELDS.items():
        if cfg.get(key):
            page.fill(f"#{field}", str(cfg[key]))
    page.select_option("#ddlEduationQualification", label=cfg["education"])
    page.select_option("#ddlVegetarianOrNon", label=cfg["diet"])
    for input_id, path in files.items():
        page.set_input_files(f"#{input_id}", path)
    page.check("#chkIAgree")

    shot = ROOT / f"filled_{datetime.now():%H%M%S}.png"
    page.screenshot(path=str(shot), full_page=True)
    log(f"Form filled, screenshot: {shot.name}")
    if dry_run:
        log("Dry run: not submitting.")
        if sys.stdin.isatty():
            log("Review the browser window; press Enter to close.")
            input()
        return True

    dialogs = []
    page.once("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=90_000):
            page.click("#btnSubmit")
    except PWTimeout:
        log("No page load after Submit (client-side validation?)")
    time.sleep(1)
    shot = ROOT / f"result_{datetime.now():%H%M%S}.png"
    page.screenshot(path=str(shot), full_page=True)
    body = page.inner_text("body")
    (ROOT / "last_result.txt").write_text("\n".join(dialogs) + "\n----\n" + body)
    error = " ".join(t.strip() for t in page.locator(".unsuccess_msg").all_inner_texts()).strip()
    msg = " | ".join(dialogs + ([error] if error else []))
    if "mobile" in error.lower():
        # Site claims this mobile already applied; this form is flaky, so keep retrying anyway.
        note = (" An earlier submit in this run may have gone through; check SMS/email."
                if fill_and_submit.clicked else "")
        log(f"Site says: '{error}'. Retrying anyway.{note}")
    fill_and_submit.clicked = True
    success = not error and (page.locator(".success_msg:visible").count() > 0 or any(
        w in (msg + body).lower() for w in ("successfully", "application no", "registration no")))
    log(f"{'SUCCESS' if success else 'Not confirmed'}: {msg[:200]}  (screenshot {shot.name}, text in last_result.txt)")
    return success


fill_and_submit.clicked = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate config and documents only")
    ap.add_argument("--dry-run", action="store_true", help="fill but don't submit")
    ap.add_argument("--poll", type=float, default=3.0, help="seconds between checks while waiting")
    ap.add_argument("--retries", type=int, default=0, help="max submit attempts (0 = keep trying until success)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="dry run that doesn't wait for seats (works before batches are published)")
    args = ap.parse_args()
    args.dry_run = args.dry_run or args.test

    cfg = json.loads((ROOT / "config.json").read_text())
    log("Checking documents:")
    files = prepare_files(cfg)
    if args.check:
        log("All good.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(30_000)
        tracker = SlotTracker()  # kept across submit retries
        for attempt in (range(1, args.retries + 1) if args.retries else itertools.count(1)):
            wait_for_seats(page, cfg, args.poll, tracker, no_wait=args.test)
            try:
                if fill_and_submit(page, cfg, files, args.dry_run):
                    break
            except Exception as e:  # timeout, error page, missing element: retry rather than crash
                log(f"Error during fill/submit ({type(e).__name__}): {str(e).splitlines()[0][:200]}")
            log(f"Submit attempt {attempt} not confirmed; retrying. Check result_*.png before it does.")
            time.sleep(args.poll)
        else:
            log("Gave up after max retries. Check screenshots and last_result.txt.")
            sys.exit(1)
        if not args.dry_run:
            log("Done. Leaving browser open 5 min so you can save the confirmation page.")
            time.sleep(300)
        browser.close()


if __name__ == "__main__":
    main()
