"""Auto-fill and submit the IISM Gulmarg admission form with direct HTTP requests (no browser).

Same steps as apply.py (course -> level -> batch -> state -> DOB -> submit) and the same
batch-preference rules, but each step is a single ~0.2s request. The ASP.NET tokens
(__VIEWSTATE / __EVENTVALIDATION) are refreshed from every response, which is why a
saved cURL can't be replayed. If the final submit hits a server/network error, the exact
same submit request is resent (up to `replay_submits` times) before redoing the steps.

  .venv/bin/python apply_http.py --check     # validate config + documents only
  .venv/bin/python apply_http.py --test      # run the steps now, don't wait for seats, don't submit
  .venv/bin/python apply_http.py --dry-run   # wait for seats, build the submit, don't send it
  .venv/bin/python apply_http.py             # wait for seats, submit
"""
import mimetypes
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import common
from common import FILE_FIELDS, ROOT, TEXT_FIELDS, URL, classify, judge, log, match_option, stamp

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
SKIP_TYPES = {"submit", "file", "checkbox", "radio", "button", "image", "reset"}
STATIC_ALERTS = ("File Size Too Large",)  # alert() text that is always in the page source


class PostbackError(Exception):
    pass


class HttpClient:
    def __init__(self, cfg):
        self.url = URL.format(mobile=cfg["mobile"])
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA, "Origin": "https://admission.iismgulmarg.in", "Referer": self.url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        self.soup, self.fields, self.html = None, {}, ""
        self.replays = cfg.get("replay_submits", 3)

    # --- page state -------------------------------------------------------------------------

    def _load(self, resp):
        if resp.status_code >= 500 or "Invalid postback or callback argument" in resp.text:
            raise PostbackError(f"server error {resp.status_code}"
                                + (" (invalid postback)" if "Invalid postback" in resp.text else ""))
        resp.raise_for_status()
        self.html = resp.text
        self.soup = BeautifulSoup(resp.text, "html.parser")
        self.fields = self._serialize()

    def _serialize(self):
        """What a browser would post for the current page: every enabled field and its value."""
        d = {}
        form = self.soup.select_one("form")
        for el in form.select("input"):
            name, typ = el.get("name"), (el.get("type") or "text").lower()
            if not name or el.has_attr("disabled") or typ in SKIP_TYPES:
                continue
            d[name] = el.get("value", "")
        for sel in form.select("select"):
            if sel.get("name") and not sel.has_attr("disabled"):
                opt = sel.select_one("option[selected]") or sel.select_one("option")
                d[sel["name"]] = opt.get("value", opt.text) if opt else ""
        for ta in form.select("textarea"):
            if ta.get("name"):
                d[ta["name"]] = ta.text
        return d

    def options(self, select_id):
        return [(o.get("value", ""), o.text.strip()) for o in self.soup.select(f"#{select_id} option")]

    def selected_text(self, select_id):
        opt = self.soup.select_one(f"#{select_id} option[selected]") or self.soup.select_one(f"#{select_id} option")
        return opt.text.strip() if opt else ""

    def value(self, input_id):
        el = self.soup.select_one(f"#{input_id}")
        return (el.get("value", "") if el else "").strip()

    @staticmethod
    def _multipart(fields, files=None):
        parts = [(k, (None, v)) for k, v in fields.items()]
        return parts + list(files or [])

    def get(self):
        self._load(self.s.get(self.url, timeout=30))

    def postback(self, target, **values):
        data = {**self.fields, **values, "__EVENTTARGET": target, "__EVENTARGUMENT": ""}
        self._load(self.s.post(self.url, files=self._multipart(data), timeout=30))

    def pick(self, select_id, wanted):
        opts = self.options(select_id)
        match = match_option([t for _, t in opts], wanted)
        if not match:
            return None, [t for _, t in opts]
        if self.selected_text(select_id) != match:
            value = next(v for v, t in opts if t == match)
            self.postback(select_id, **{select_id: value})
        return match, None

    # --- client interface used by common.py -------------------------------------------------

    def load_level(self, cfg):
        self.get()
        got, opts = self.pick("ddlCourse", cfg["course"])
        if not got:
            raise LookupError(f"course not listed; options: {opts}")
        got, opts = self.pick("ddlCourseLevel", cfg["course_level"])
        if not got:
            raise LookupError(f"level not listed yet; options: {opts}")
        return [t for _, t in self.options("ddlScheduleCourse")]

    def select_state(self, cfg):
        self.pick("ddlState", cfg["state"])

    def read_slot(self, cfg, sched):
        try:
            self.pick("ddlScheduleCourse", sched)
            self.select_state(cfg)
        except requests.Timeout:
            return "unclear", "timeout"
        except Exception as e:  # error page, connection drop, etc.
            return "unclear", type(e).__name__
        return classify(self.selected_text("ddlScheduleCourse"), sched,
                        self.value("txtAvailableSeat"), self.value("hdfMaximumAge"))

    def fill_and_submit(self, cfg, files, dry_run):
        self.postback("txtDateOfBirth", txtDateOfBirth=cfg["dob"])  # server computes age/fee
        data = dict(self.fields)
        data.update({field: str(cfg.get(key) or "") for key, field in TEXT_FIELDS.items()})
        data.update({
            "txtDateOfBirth": cfg["dob"],
            "ddlEduationQualification": cfg["education"],
            "ddlVegetarianOrNon": cfg["diet"],
            "chkIAgree": "on", "btnSubmit": "Submit",
            "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        })
        uploads = []
        for input_id, _, _ in FILE_FIELDS.values():
            if input_id in files:
                p = Path(files[input_id])
                mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                uploads.append((input_id, (p.name, p.read_bytes(), mime)))
            else:
                uploads.append((input_id, ("", b"", "application/octet-stream")))  # as a browser sends it
        log(f"Submit built: course fee {data.get('txtCourseFee')}, seats {data.get('txtAvailableSeat')}, "
            f"{sum(1 for _, (n, _, _) in uploads if n)} files")
        if dry_run:
            (ROOT / "http_dry_run_fields.txt").write_text(
                "\n".join(f"{k} = {v[:80]}" for k, v in data.items() if not k.startswith("__")))
            log("Dry run: not submitting. Fields written to http_dry_run_fields.txt")
            return True

        parts = self._multipart(data, uploads)
        for n in range(1 + self.replays):
            try:
                resp = self.s.post(self.url, files=parts, timeout=90)
                if resp.status_code < 500:
                    break
                log(f"Submit got server error {resp.status_code}")
            except requests.RequestException as e:
                log(f"Submit network error: {type(e).__name__}")
                resp = None
            if n < self.replays:
                log(f"Resending the same submit ({n + 1}/{self.replays})")
                time.sleep(2)
        if resp is None:
            return False

        out = ROOT / f"http_result_{stamp()}.html"
        out.write_text(resp.text)
        soup = BeautifulSoup(resp.text, "html.parser")
        dialogs = [m for m in re.findall(r"alert\(\s*['\"](.+?)['\"]\s*\)", resp.text)
                   if not any(s in m for s in STATIC_ALERTS)]
        error = " ".join(e.get_text(" ", strip=True) for e in soup.select(".unsuccess_msg")).strip()
        success_el = any(e.get_text(strip=True) for e in soup.select(".success_msg"))
        return judge(error, dialogs, soup.get_text(" ", strip=True), success_el, out.name)


def main():
    common.TAG = " [http]"
    args = common.parse_args(__doc__)
    cfg = common.load_config()
    log("Checking documents:")
    files = common.prepare_files(cfg)
    if args.check:
        log("All good.")
        return
    if common.run(HttpClient(cfg), cfg, files, args) and not args.dry_run:
        log("Done. Open the saved http_result_*.html to see the confirmation page.")


if __name__ == "__main__":
    main()
