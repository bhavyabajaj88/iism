"""Shared pieces for apply.py (browser) and apply_http.py (direct HTTP).

Both scripts implement a small "client" with the same four methods, and this module
drives them with one copy of the seat-waiting, slot-preference and retry logic:

  client.load_level(cfg)          -> list of schedule names for the configured course/level
  client.read_slot(cfg, sched)    -> ("open", n) | ("full", 0) | ("unclear", why)
  client.select_state(cfg)        -> select the state dropdown (no-op if already selected)
  client.fill_and_submit(cfg, files, dry_run) -> True on confirmed success
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

ROOT = Path(__file__).parent
URL = "https://admission.iismgulmarg.in/admission_form.aspx?mobile={mobile}"
TARGET_BYTES = 450_000         # page text says signatures/photo must be <= 500 KB (hard limit 1 MB)
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

TAG = ""  # set by each script so logs are distinguishable when both run


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}]{TAG} {msg}", flush=True)


def stamp():
    return datetime.now().strftime("%H%M%S")


def load_config():
    return json.loads((ROOT / "config.json").read_text())


def compress_image(src: Path, out_dir: Path) -> Path:
    """Re-encode as JPEG with macOS `sips`, shrinking until under TARGET_BYTES."""
    dst = out_dir / (src.stem + ".jpg")
    for max_px, quality in [(2000, 85), (1600, 80), (1400, 70), (1200, 60), (1000, 50), (800, 40)]:
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
                        "-Z", str(max_px), str(src), "--out", str(dst)],
                       check=True, capture_output=True)
        if dst.stat().st_size <= TARGET_BYTES:
            return dst
    raise SystemExit(f"Could not compress {src} below {TARGET_BYTES // 1000} KB")


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


def match_option(options, wanted):
    """First option text containing `wanted` (case-insensitive), skipping "Select ..." placeholders."""
    return next((o for o in options if wanted.lower() in o.lower() and not o.lower().startswith("select")), None)


def filter_schedules(cfg, options):
    return [o for o in options if not o.lower().startswith("select")
            and not any(x.lower() in o.lower() for x in cfg.get("schedule_exclude", []))]


def classify(selected, sched, raw_seats, max_age):
    """Turn what the page shows after selecting `sched` into open / full / unclear.

    The seats box defaults to "0", so a failed/partial postback looks exactly like a full batch.
    A zero only counts as "full" when the page provably reflects this schedule: the dropdown
    shows it and the server filled in its age limits (0 until a schedule is loaded).
    """
    if selected != sched:
        return "unclear", f"dropdown shows '{selected[:30]}'"
    if not raw_seats.isdigit():
        return "unclear", f"seats box '{raw_seats}'"
    if int(raw_seats) > 0:
        return "open", int(raw_seats)  # a positive count can't come from the default
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


def wait_for_seats(client, cfg, poll, tracker, no_wait=False):
    """Reload until a preference is open AND every higher preference is confirmed full.

    cfg["schedule"]: substrings in preference order. The first `top_choices` are the main
    choices: moving from one to the next needs `confirm_reads` consecutive verified-full reads
    of every choice above it. Moving past the top choices needs `deep_confirm_reads` reads of
    each, spread over at least `deep_confirm_seconds`. A batch that isn't listed is never "full".
    """
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
            schedules = filter_schedules(cfg, client.load_level(cfg))
            if not schedules and no_wait:
                log("No schedules published; continuing for test")
                client.select_state(cfg)
                return
            report = []
            for i, pref in enumerate(prefs):
                # Lower choices are only read once the top ones are close to deep-confirmed.
                if i >= top and not all(tracker.confirmed(p, reads) for p in prefs[:top]):
                    break
                sched = match_option(schedules, pref)
                if not sched:
                    tracker.record(pref, "unlisted")
                    report.append(f"{pref}=not listed")
                    continue
                status, info = client.read_slot(cfg, sched)
                tracker.record(pref, status)
                report.append(f"{pref}={status}({info})")
                if status == "open" or (no_wait and status != "unclear"):
                    if allowed(i) or no_wait:
                        log(f"TAKING '{sched}' ({info} seats). Checks: {'; '.join(report)}")
                        return
                    # Open, but a higher choice isn't proven full yet: recheck immediately.
                    waiting_on_confirm = True
            log(f"#{attempt} {'; '.join(report) or 'no batches published yet'}"
                f" | streaks: {tracker.summary(prefs[:top])}"
                + (" | lower choice open, re-confirming higher ones" if waiting_on_confirm else ""))
        except Exception as e:  # not listed yet, timeout, network drop, server error page
            log(f"#{attempt} waiting: {str(e).splitlines()[0][:200]}")
        if not waiting_on_confirm:
            time.sleep(poll + random.uniform(0, 1))


class Outcome:
    """Tracks whether this process has clicked Submit before (affects how errors are read)."""
    clicked = False


def judge(error, dialogs, body, success_visible, result_file):
    """Decide success from the post-submit page. Never stops on the mobile-number error."""
    msg = " | ".join(dialogs + ([error] if error else []))
    if "mobile" in error.lower():
        note = (" An earlier submit in this run may have gone through; check SMS/email."
                if Outcome.clicked else "")
        log(f"Site says: '{error}'. Retrying anyway.{note}")
    Outcome.clicked = True
    success = not error and (success_visible or any(
        w in (msg + body).lower() for w in ("successfully", "application no", "registration no")))
    log(f"{'SUCCESS' if success else 'Not confirmed'}: {msg[:200]}  (saved {result_file})")
    return success


def parse_args(prog_doc):
    ap = argparse.ArgumentParser(description=prog_doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate config and documents only")
    ap.add_argument("--dry-run", action="store_true", help="fill but don't submit")
    ap.add_argument("--test", action="store_true",
                    help="dry run that doesn't wait for seats (works before batches are published)")
    ap.add_argument("--poll", type=float, default=3.0, help="seconds between checks while waiting")
    ap.add_argument("--retries", type=int, default=0, help="max submit attempts (0 = keep trying until success)")
    ap.add_argument("--headless", action="store_true", help="browser script only: hide the window")
    args = ap.parse_args()
    args.dry_run = args.dry_run or args.test
    return args


def run(client, cfg, files, args):
    """Wait for a seat, fill, submit; keep retrying until success. Returns True on success."""
    tracker = SlotTracker()  # kept across submit retries
    for attempt in (range(1, args.retries + 1) if args.retries else itertools.count(1)):
        wait_for_seats(client, cfg, args.poll, tracker, no_wait=args.test)
        try:
            if client.fill_and_submit(cfg, files, args.dry_run):
                return True
        except Exception as e:  # timeout, error page, missing element: retry rather than crash
            log(f"Error during fill/submit ({type(e).__name__}): {str(e).splitlines()[0][:200]}")
        log(f"Submit attempt {attempt} not confirmed; retrying.")
        time.sleep(args.poll)
    log("Gave up after max retries. Check saved result files.")
    sys.exit(1)
