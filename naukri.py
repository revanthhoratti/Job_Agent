"""Naukri.com: find jobs and apply, in a real browser window that YOU are logged into.

- Naukri blocks plain HTTP requests, so discovery also runs in the browser.
- First run: a Chromium window opens; log in to Naukri yourself. The session is kept in
  ./.browser_naukri so you only do this once. Your password is never read or stored.
- Applying: clicks Apply. Many jobs then show a short chat-style questionnaire; each
  question is answered by the same LLM answerer used for LinkedIn (form_answerer.py),
  and anything unsure / sensitive (salary, notice period...) is put to you first.
- Jobs that say "Apply on company site" are marked 'external' for you to do by hand.
- With auto_submit: false (default) you confirm every application in the terminal,
  because on Naukri a single click on Apply sends the application.
"""
import json
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
PROFILE_DIR = str(ROOT / ".browser_naukri")
AGE_OPTIONS = [1, 3, 7, 15, 30]  # values Naukri's "freshness" filter accepts
MAX_QUESTIONS = 20


def _pause(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _search_url(keywords: str, location: str, days: int, page_no: int) -> str:
    slug = _slug(keywords) + "-jobs"
    if location and location.strip().lower() not in ("india", "all india", "any"):
        slug += "-in-" + _slug(location)
    if page_no > 1:
        slug += f"-{page_no}"
    age = next((a for a in AGE_OPTIONS if a >= days), 30)
    return f"https://www.naukri.com/{slug}?jobAge={age}"


def _job_id(raw_id: str, url: str) -> str:
    digits = re.sub(r"\D", "", raw_id or "")
    if not digits:
        m = re.findall(r"(\d{8,})", url.split("?")[0])
        digits = m[-1] if m else ""
    return f"n{digits}" if digits else ""


CARD_JS = """
() => {
  const cards = document.querySelectorAll('div.srp-jobtuple-wrapper, div.cust-job-tuple, article.jobTuple');
  return Array.from(cards).map(c => {
    const t  = c.querySelector('a.title, a[class*=title]');
    const co = c.querySelector('a.comp-name, a[class*=comp-name], span[class*=comp-name]');
    const lo = c.querySelector('span.locWdth, span[class*=locWdth], li.location, span[class*=loc]');
    return {
      rawId: c.getAttribute('data-job-id') || '',
      title: t ? t.innerText.trim() : '',
      url: t ? t.href : '',
      company: co ? co.innerText.trim() : '',
      location: lo ? lo.innerText.trim() : '',
    };
  });
}
"""

DESC_SELECTORS = [
    "div[class*=dang-inner-html]",
    "section[class*=job-desc]",
    "div[class*=JDC]",
    "div.job-desc",
]


def _description(page) -> str:
    for sel in DESC_SELECTORS:
        loc = page.locator(sel)
        if loc.count():
            try:
                text = loc.first.inner_text(timeout=3000).strip()
                if len(text) > 80:
                    return text[:8000]
            except Exception:
                pass
    return ""


def _blocked(page) -> bool:
    try:
        title = page.title().lower()
    except Exception:
        return False
    return "access denied" in title or "captcha" in title or "just a moment" in title


def _debug_dump(page, job_id: str, tag: str):
    folder = ROOT / "out" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(folder / f"debug_{tag}.png"))
    except Exception:
        pass
    lines = [f"url: {page.url}"]
    try:
        for b in page.locator("button:visible, [role=button]:visible").all()[:60]:
            text = (b.inner_text() or "").strip().replace("\n", " ")[:60]
            lines.append(f"{text!r} | id={b.get_attribute('id')!r} | class={(b.get_attribute('class') or '')[:60]!r}")
    except Exception:
        pass
    (folder / f"debug_{tag}.txt").write_text("\n".join(lines) + "\n")
    print(f"   debug saved: out/{job_id}/debug_{tag}.png and .txt")


def _ensure_login(page):
    page.goto("https://www.naukri.com/mnjuser/homepage")
    _pause(2, 3)
    if re.search(r"nlogin|/login", page.url, re.I) or page.get_by_role("link", name=re.compile(r"^\s*Login\s*$", re.I)).count():
        input("Log in to Naukri in the browser window, then press Enter here... ")


# ----------------------------------------------------------------------------- discover
def discover(searches: list[str], locations: list[str], days: int, limit: int, keep) -> list[dict]:
    """Search Naukri. `keep(job) -> bool` filters (title, company, already-known) BEFORE the
    job page is opened for its description, so we only spend time on jobs worth scoring."""
    found: dict[str, dict] = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = ctx.new_page()
        _ensure_login(page)

        for kw in searches:
            for loc in locations:
                print(f"Searching Naukri: '{kw}' in {loc or 'India'}")
                got = 0
                for page_no in range(1, 6):
                    page.goto(_search_url(kw, loc, days, page_no))
                    _pause(3, 5)
                    if _blocked(page):
                        print("  Naukri showed a bot check. Solve it in the browser window, then press Enter.")
                        input("  ... ")
                    try:
                        page.wait_for_selector("div.srp-jobtuple-wrapper, div.cust-job-tuple, article.jobTuple", timeout=8000)
                    except Exception:
                        pass
                    cards = page.evaluate(CARD_JS)
                    if not cards:
                        if page_no == 1:
                            _debug_dump(page, "naukri_search", "no_cards")
                        break
                    for c in cards:
                        jid = _job_id(c["rawId"], c["url"])
                        if not jid or not c["title"] or jid in found:
                            continue
                        job = {"id": jid, "source": "naukri", "title": c["title"], "company": c["company"],
                               "location": c["location"], "url": c["url"].split("?")[0]}
                        if keep(job):
                            found[jid] = job
                            got += 1
                    if got >= limit:
                        break
                    _pause(2, 4)

        for job in found.values():
            try:
                page.goto(job["url"])
                _pause(2.5, 4)
                job["description"] = _description(page)
            except Exception as e:
                job["description"] = ""
                print(f"  couldn't read description for {job['title']}: {e}")
        ctx.close()
    return list(found.values())


# ------------------------------------------------------------------------------ apply
APPLY_BTN = "#apply-button"
COMPANY_BTN = "#company-site-button"
ALREADY = "#already-applied"
DRAWER_SELECTORS = ["[class*=chatbot_Drawer]", "[class*=chatbot_DrawerContentWrapper]", "[class*=chatbot]"]
BOT_MSG = "[class*=botMsg], [class*=bot-msg], [class*=botItem]"
OPTION_SELECTORS = [
    "[class*=ssrc__label]",
    "[class*=chatbot_Chip]",
    "[class*=Chip]:visible",
    "label:visible",
]
INPUT_SELECTORS = ["div[contenteditable=true]", "input[type=text]:visible", "input[type=number]:visible", "textarea:visible"]
SEND_SELECTORS = ["div[class*=sendMsg]", "button:has-text('Save')", "button:has-text('Send')", "button:has-text('Submit')"]
SUCCESS_RE = re.compile(r"successfully applied|application (has been )?(sent|submitted)|you have applied|applied successfully", re.I)


def _success(page) -> bool:
    try:
        if page.locator(ALREADY).count():
            return True
        if page.get_by_role("button", name=re.compile(r"^\s*Applied\s*$", re.I)).count():
            return True
        return bool(SUCCESS_RE.search(page.locator("body").inner_text(timeout=3000)))
    except Exception:
        return False


def _drawer(page):
    for sel in DRAWER_SELECTORS:
        loc = page.locator(sel)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                pass
    return None


def _last_question(drawer) -> str:
    try:
        msgs = drawer.locator(BOT_MSG)
        if msgs.count():
            text = msgs.last.inner_text(timeout=2000).strip()
            if text:
                return " ".join(text.split())
    except Exception:
        pass
    try:  # fallback: last few non-empty lines of the drawer
        lines = [l.strip() for l in drawer.inner_text(timeout=2000).splitlines() if l.strip()]
        return " ".join(lines[-3:])
    except Exception:
        return ""


def _options(drawer) -> list[tuple[str, object]]:
    for sel in OPTION_SELECTORS:
        loc = drawer.locator(sel)
        found, seen = [], set()
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                text = " ".join(el.inner_text(timeout=1000).split())
            except Exception:
                continue
            if text and text.lower() not in seen and len(text) < 120:
                seen.add(text.lower())
                found.append((text, el))
        if found:
            return found
    return []


def _input_box(drawer):
    for sel in INPUT_SELECTORS:
        loc = drawer.locator(sel)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                pass
    return None


def _send(page, drawer):
    for sel in SEND_SELECTORS:
        loc = drawer.locator(sel)
        for i in range(loc.count()):
            try:
                if loc.nth(i).is_visible():
                    loc.nth(i).click(timeout=2000)
                    return True
            except Exception:
                pass
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _questionnaire(page, answerer, qa: list) -> str:
    last, repeats = None, 0
    for _ in range(MAX_QUESTIONS):
        _pause(1.5, 2.5)
        if _success(page):
            return "applied"
        drawer = _drawer(page)
        if drawer is None:
            return "applied" if _success(page) else "failed:unconfirmed"

        question = _last_question(drawer)
        options = _options(drawer)
        box = _input_box(drawer)
        repeats = repeats + 1 if question == last else 0
        last = question

        if repeats >= 2 or not question or (not options and box is None):
            input(f"   Naukri needs your help with: {question or 'the current step'!r}. "
                  "Answer it in the browser, then press Enter... ")
            repeats = 0
            continue

        if options:
            res = answerer(question, "radio", [t for t, _ in options])
        else:
            kind = "number" if re.search(r"how many|years|lpa|lakh|ctc|salary|notice|number of", question, re.I) else "text"
            res = answerer(question, kind, None)
        if not res or not res["answer"]:
            input("   I can't answer that. Answer it in the browser, then press Enter... ")
            continue
        qa.append({"question": question, "answer": res["answer"], "source": res.get("source"),
                   "confidence": res.get("confidence"), "confirmed_by_you": res.get("user_confirmed")})
        print(f"   {question[:70]} -> {res['answer'][:40]}")

        if options:
            for text, el in options:
                if text.lower() == res["answer"].lower():
                    el.click()
                    break
            _pause(0.5, 1.0)
            if _has_send(drawer):  # some option lists need Save; chips usually submit on click
                _send(page, drawer)
        else:
            try:
                box.click()
                if box.evaluate("e => e.isContentEditable"):
                    page.keyboard.type(res["answer"], delay=40)
                else:
                    box.fill(res["answer"])
            except Exception:
                pass
            _send(page, drawer)
    return "failed:too_many_questions"


def _has_send(drawer) -> bool:
    for sel in SEND_SELECTORS[1:]:
        try:
            if drawer.locator(sel).count():
                return True
        except Exception:
            pass
    return False


def _write_log(job: dict, qa: list, status: str):
    folder = ROOT / "out" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "apply_log.json").write_text(json.dumps(
        {"job": job["title"], "company": job["company"], "url": job["url"], "status": status, "questions": qa}, indent=2))


def _apply_one(page, job: dict, answerer, auto_submit: bool) -> str:
    page.goto(job["url"])
    _pause(3, 5)
    if _blocked(page):
        input("   Naukri showed a bot check. Solve it in the browser, then press Enter... ")

    if page.locator(ALREADY).count():
        print("   already applied on Naukri")
        return "applied"
    btn = page.locator(APPLY_BTN)
    if not btn.count():
        if page.locator(COMPANY_BTN).count() or page.get_by_role("button", name=re.compile("company site", re.I)).count():
            print("   applies on the company's site, skipped (apply manually via the link)")
            return "external"
        btn = page.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I))
        if not btn.count():
            _debug_dump(page, job["id"], "no_apply_button")
            return "failed:no_apply_button"

    if not auto_submit:
        ok = input(f"   Apply to this job now? (score {job.get('score', '-')}) [y/N] ").strip().lower() == "y"
        if not ok:
            return "skipped"

    qa: list = []
    btn.first.click()
    status = _questionnaire(page, answerer, qa)
    if status != "applied":
        _debug_dump(page, job["id"], "not_confirmed")
    _write_log(job, qa, status)
    return status


def apply_jobs(jobs: list[dict], answerer, auto_submit: bool) -> dict:
    """Returns {job_id: status}. Status: applied | external | skipped | failed:<why>."""
    results = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = ctx.new_page()
        _ensure_login(page)
        for job in jobs:
            print(f"\n-> [Naukri] {job['title']} @ {job['company']}")
            try:
                answerer.set_job(job)
                results[job["id"]] = _apply_one(page, job, answerer, auto_submit)
            except Exception as e:
                results[job["id"]] = f"failed:{type(e).__name__}"
                print(f"   failed: {e}")
            print(f"   {results[job['id']]}")
            _pause(8, 20)
        ctx.close()
    return results
