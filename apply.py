"""Auto-fill and submit the IISM Gulmarg admission form using a real (Chromium) browser.

Waits for seats in your preferred batch, fills the form and clicks Submit. If a submit
fails (any reason, including the site's mobile-number error) it keeps trying until one
succeeds or you press Ctrl+C. See apply_http.py for the faster browser-free version.

  .venv/bin/python apply.py --check       # validate config + documents only
  .venv/bin/python apply.py --test        # fill everything now, don't wait for seats, don't submit
  .venv/bin/python apply.py --dry-run     # wait for seats, fill, don't submit
  .venv/bin/python apply.py               # wait for seats, fill, submit
"""
import sys
import time

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import common
from common import ROOT, TEXT_FIELDS, URL, classify, judge, log, match_option, stamp


class BrowserClient:
    def __init__(self, page):
        self.page = page

    def pick(self, select_id, wanted):
        """Select the option whose text contains `wanted`; waits for the postback."""
        options = self.page.locator(f"#{select_id} option").all_inner_texts()
        match = match_option(options, wanted)
        if not match:
            return None, options
        if self.page.locator(f"#{select_id} option:checked").inner_text() == match:
            return match, options  # already selected: selecting again fires no postback
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.select_option(f"#{select_id}", label=match)
        return match, options

    def load_level(self, cfg):
        self.page.goto(URL.format(mobile=cfg["mobile"]), wait_until="domcontentloaded", timeout=30_000)
        got, opts = self.pick("ddlCourse", cfg["course"])
        if not got:
            raise LookupError(f"course not listed; options: {opts}")
        got, opts = self.pick("ddlCourseLevel", cfg["course_level"])
        if not got:
            raise LookupError(f"level not listed yet; options: {opts}")
        return self.page.locator("#ddlScheduleCourse option").all_inner_texts()

    def select_state(self, cfg):
        self.pick("ddlState", cfg["state"])

    def read_slot(self, cfg, sched):
        page = self.page
        try:
            self.pick("ddlScheduleCourse", sched)
            self.select_state(cfg)
            selected = page.locator("#ddlScheduleCourse option:checked").inner_text()
            raw = page.input_value("#txtAvailableSeat").strip()
            max_age = page.input_value("#hdfMaximumAge").strip()
        except PWTimeout:
            return "unclear", "timeout"
        except Exception as e:  # error page, missing elements, etc.
            return "unclear", type(e).__name__
        return classify(selected, sched, raw, max_age)

    def fill_and_submit(self, cfg, files, dry_run):
        page = self.page
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

        shot = ROOT / f"browser_filled_{stamp()}.png"
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
        shot = ROOT / f"browser_result_{stamp()}.png"
        page.screenshot(path=str(shot), full_page=True)
        body = page.inner_text("body")
        (ROOT / "browser_last_result.txt").write_text("\n".join(dialogs) + "\n----\n" + body)
        error = " ".join(t.strip() for t in page.locator(".unsuccess_msg").all_inner_texts()).strip()
        return judge(error, dialogs, body, page.locator(".success_msg:visible").count() > 0, shot.name)


def main():
    common.TAG = " [browser]"
    args = common.parse_args(__doc__)
    cfg = common.load_config()
    log("Checking documents:")
    files = common.prepare_files(cfg)
    if args.check:
        log("All good.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(30_000)
        common.run(BrowserClient(page), cfg, files, args)
        if not args.dry_run:
            log("Done. Leaving browser open 5 min so you can save the confirmation page.")
            time.sleep(300)
        browser.close()


if __name__ == "__main__":
    main()
