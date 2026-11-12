"""Fill LinkedIn Easy Apply forms in a real browser window that YOU are logged into.

- First run: a Chromium window opens; log in to LinkedIn yourself (the session is
  kept in ./.browser so you only do this once).
- Only Easy Apply jobs are handled. External apply links are marked 'external'.
- Every question is read and answered by the LLM (see form_answerer.py) from your
  profile and answers.yaml. Anything it is unsure about, or can't support from your
  facts, is put to you in the terminal instead of being guessed.
- Multi-page forms: each page is filled, checked for errors, and only then saved by
  clicking Next. The page is confirmed to have advanced before the agent continues.
  Progress per page is written to out/<job_id>/apply_log.json.
- If an application is abandoned part-way, it is saved as a LinkedIn draft
  (the "Save" button), not discarded, so completed pages are kept.
- With auto_submit: false (default) you confirm every submission in the terminal.
"""
import json
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
PROFILE_DIR = str(ROOT / ".browser")
MAX_PAGES = 12
MAX_NEXT_RETRIES = 3


def _pause(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))


def _dedupe(text: str) -> str:
    """LinkedIn repeats question text for screen readers ('Question\\nQuestion'); keep it once."""
    lines = []
    for line in (t.strip() for t in text.splitlines()):
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    half = len(lines) // 2
    if half and len(lines) % 2 == 0 and lines[:half] == lines[half:]:
        lines = lines[:half]
    text = " ".join(lines)
    # Same text twice with no line break between (adjacent <span>s): "Q?Q?" or "Q? Q?"
    for sep in ("", " "):
        body = len(text) - len(sep)
        if body > 0 and body % 2 == 0:
            h = body // 2
            if text[:h] == text[h + len(sep):] and text[h:h + len(sep)] == sep:
                return text[:h]
    return text


def _option_texts(loc) -> list[str]:
    return [t.strip() for t in loc.all_inner_texts() if t.strip()]


def _label_for(page_or_dialog, el) -> str:
    try:
        el_id = el.get_attribute("id")
        if el_id:
            lab = page_or_dialog.locator(f'label[for="{el_id}"]')
            if lab.count():
                return _dedupe(lab.first.inner_text())
        aria = el.get_attribute("aria-label")
        if aria:
            return aria
    except Exception:
        pass
    return ""


def _fill_step(dialog, answerer, resume: str) -> dict:
    """Fill the current page by letting `answerer` read each question.

    Returns {'filled': [labels], 'unanswered': [labels], 'qa': [question/answer records]}.
    """
    filled, unanswered, qa = [], [], []

    def done(question, kind, res):
        filled.append(question)
        qa.append({"question": question, "kind": kind, "answer": res["answer"],
                   "source": res.get("source"), "confidence": res.get("confidence"),
                   "confirmed_by_you": res.get("user_confirmed")})

    for f in dialog.locator("input[type=file]").all():
        try:
            f.set_input_files(resume)
            filled.append("resume upload")
        except Exception:
            pass

    # --- text boxes, numbers, free-text areas ----------------------------------
    fields = "input[type=text],input[type=number],input[type=tel],input[type=email],textarea"
    for el in dialog.locator(fields).all():
        try:
            if el.input_value():
                continue
            label = _label_for(dialog, el)
            if not label:
                continue
            if el.evaluate("e => e.tagName") == "TEXTAREA":
                kind = "textarea"
            elif el.get_attribute("type") == "number" or re.search(r"how many|years|number of", label, re.I):
                kind = "number"
            else:
                kind = "text"
            res = answerer(label, kind, None)
            if res and res["answer"]:
                el.fill(res["answer"])
                _pick_suggestion(dialog, el, label)
                done(label, kind, res)
            else:
                unanswered.append(label)
        except Exception:
            pass

    # --- dropdowns -------------------------------------------------------------
    for sel in dialog.locator("select").all():
        try:
            current = sel.input_value()
            if current and "select" not in current.lower():
                continue
            label = _label_for(dialog, sel)
            if not label:
                continue
            res = answerer(label, "select", _option_texts(sel.locator("option")))
            if res and res["answer"]:
                sel.select_option(label=res["answer"])
                done(label, "select", res)
            else:
                unanswered.append(label)
        except Exception:
            pass

    # --- radio groups (Yes/No and multiple choice) -----------------------------
    for fs in dialog.locator("fieldset").all():
        try:
            legend = _dedupe(fs.locator("legend").first.inner_text())
            if fs.locator("input[type=checkbox]").count():
                unanswered.append(legend)  # tick-boxes / consent: left for you
                continue
            if fs.locator("input:checked").count():
                continue
            options = _dedupe_options(_option_texts(fs.locator("label")))
            res = answerer(legend, "radio", options)
            if res and res["answer"]:
                pattern = re.compile(rf"^\s*{re.escape(res['answer'])}\s*$", re.I)
                fs.locator("label").filter(has_text=pattern).first.click()
                done(legend, "radio", res)
            else:
                unanswered.append(legend)
        except Exception:
            pass

    return {"filled": filled, "unanswered": unanswered, "qa": qa}


def _dedupe_options(options: list[str]) -> list[str]:
    seen, out = set(), []
    for o in (_dedupe(o) for o in options):
        if o and o.lower() not in seen:
            seen.add(o.lower())
            out.append(o)
    return out


def _has_errors(dialog) -> bool:
    return dialog.locator(".artdeco-inline-feedback--error, [aria-invalid=true]").count() > 0


def _page_signature(dialog) -> str:
    """Heading + progress value, used to confirm that Next really moved to a new page."""
    heading, progress = "", ""
    try:
        heading = dialog.locator("h3, h2").first.inner_text(timeout=1500).strip()
    except Exception:
        pass
    try:
        bar = dialog.locator("progress, [role=progressbar]").first
        progress = bar.get_attribute("value") or bar.get_attribute("aria-valuenow") or ""
    except Exception:
        pass
    return f"{heading}|{progress}"


DIALOG_SELECTORS = [
    "div.jobs-easy-apply-modal",
    "[data-test-modal-id='easy-apply-modal']",
    "div[role=dialog][aria-labelledby*='apply' i]",
    "div[role=dialog]",
    "div.artdeco-modal",
]
NEXT_SELECTOR = ", ".join(
    [
        "button[aria-label*='next step' i]",
        "button[aria-label*='Review your application' i]",
        "button[data-easy-apply-next-button]",
        "button[data-live-test-easy-apply-next-button]",
        "button[data-live-test-easy-apply-review-button]",
        "button:has-text('Next')",
        "button:has-text('Review')",
        "button:has-text('Continue')",
    ]
)
SUBMIT_SELECTOR = ", ".join(
    [
        "button[aria-label*='Submit application' i]",
        "button[data-live-test-easy-apply-submit-button]",
        "button:has-text('Submit application')",
    ]
)


_SCOPE_XPATH = (
    "xpath=ancestor::*[.//button[normalize-space(.)='Next' or normalize-space(.)='Review' "
    "or starts-with(normalize-space(.),'Continue') or contains(normalize-space(.),'Submit application')]][1]"
)


def _find_by_anchor(page):
    """Find the Easy Apply window without relying on its CSS classes.

    The window always has a Dismiss (X) button and a Next/Review/Submit button. The
    smallest element that contains both is the form. Works however LinkedIn builds it.
    """
    dismiss = page.locator("button[aria-label='Dismiss']")
    for i in range(dismiss.count()):
        d = dismiss.nth(i)
        try:
            if not d.is_visible():
                continue
            scope = d.locator(_SCOPE_XPATH)
            if scope.count():
                if scope.first.evaluate("e => e.tagName") in ("BODY", "HTML"):
                    continue  # stray Dismiss button outside the form; not the application window
                return scope.first
        except Exception:
            pass
    return None


def _pick_suggestion(dialog, el, label: str):
    """Typeahead fields (e.g. 'Location (city)') need a suggestion clicked, not just text."""
    try:
        is_combo = (
            el.get_attribute("role") == "combobox"
            or el.get_attribute("aria-autocomplete")
            or el.get_attribute("aria-haspopup")
            or re.search(r"location|city", label, re.I)
        )
        if not is_combo:
            return
        opt = dialog.page.locator("[role=option]:visible").first
        opt.wait_for(state="visible", timeout=3000)
        opt.click()
        _pause(0.5, 1.0)
    except Exception:
        pass  # no dropdown appeared; the field accepted plain text


def _find_dialog(page, timeout_s: float = 10.0):
    """Wait for the Easy Apply modal and return it (the visible one that has buttons)."""
    end = time.time() + timeout_s
    while time.time() < end:
        anchored = _find_by_anchor(page)
        if anchored is not None:
            return anchored
        for sel in DIALOG_SELECTORS:
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                try:
                    if el.is_visible() and el.locator("button").count():
                        return el
                except Exception:
                    pass
        time.sleep(0.5)
    return None


def _next_button(dialog):
    return dialog.locator(NEXT_SELECTOR)


def _submit_button(dialog):
    return dialog.locator(SUBMIT_SELECTOR)


def _debug_dump(page, job: dict, tag: str):
    """Save a screenshot and the visible button labels so a stuck form can be diagnosed."""
    folder = ROOT / "out" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(folder / f"debug_{tag}.png"))
    except Exception:
        pass
    lines = [f"url: {page.url}"]
    try:
        for b in page.locator("button:visible").all()[:60]:
            text = (b.inner_text() or "").strip().replace("\n", " ")[:60]
            lines.append(f"{text!r} | aria-label={b.get_attribute('aria-label')!r}")
    except Exception:
        pass
    (folder / f"debug_{tag}.txt").write_text("\n".join(lines) + "\n")
    print(f"   debug saved: out/{job['id']}/debug_{tag}.png and .txt")


def _close(page, save_draft: bool = True) -> bool:
    """Close the dialog. Saves the partial application as a draft when possible."""
    try:
        page.get_by_role("button", name=re.compile("Dismiss", re.I)).first.click(timeout=3000)
        _pause(0.8, 1.5)
    except Exception:
        return False
    if save_draft:
        try:
            page.get_by_role("button", name=re.compile(r"^\s*Save\s*$", re.I)).first.click(timeout=3000)
            return True
        except Exception:
            pass
    try:
        page.get_by_role("button", name=re.compile("Discard", re.I)).first.click(timeout=3000)
    except Exception:
        pass
    return False


def _write_log(job: dict, log: list, status: str, draft_saved: bool = False):
    folder = ROOT / "out" / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "apply_log.json").write_text(
        json.dumps(
            {"job": job["title"], "company": job["company"], "url": job["url"],
             "status": status, "draft_saved_on_linkedin": draft_saved, "pages": log},
            indent=2,
        )
    )


def _apply_one(page, job: dict, answerer, resume: str, auto_submit: bool) -> str:
    btn = page.get_by_role("button", name=re.compile(r"Easy Apply", re.I))
    if not btn.count():
        print("   not Easy Apply, skipped (apply manually via the link)")
        return "external"
    btn.first.click()
    _pause()
    dialog = _find_dialog(page)
    if dialog is None:
        _debug_dump(page, job, "no_dialog")
        return "failed:no_dialog"

    log = []
    status = f"failed:more_than_{MAX_PAGES}_pages"

    for page_no in range(1, MAX_PAGES + 1):
        signature = _page_signature(dialog)
        result = _fill_step(dialog, answerer, resume)
        print(f"   page {page_no}: filled {len(result['filled'])} field(s)"
              + (f", needs you: {', '.join(result['unanswered'][:4])}" if result["unanswered"] else ""))
        _pause()

        # --- last page: review and submit ---------------------------------------
        submit = _submit_button(dialog)
        if submit.count():
            if _has_errors(dialog):
                input("   Fix the highlighted fields in the browser, then press Enter... ")
            try:
                dialog.locator("label[for*=follow-company]").first.click(timeout=1500)
            except Exception:
                pass
            go = True
            if not auto_submit:
                go = input("   Review the form. Submit this application? [y/N] ").strip().lower() == "y"
            log.append({"page": page_no, "title": signature.split("|")[0], **result, "action": "submit" if go else "declined"})
            if go:
                submit.first.click()
                _pause(2, 3)
                status = "applied"
            else:
                saved = _close(page, save_draft=True)
                _write_log(job, log, "skipped", saved)
                return "skipped"
            break

        # --- middle page: save it by clicking Next, confirm it advanced ---------
        nxt = _next_button(dialog)
        if not nxt.count():
            _debug_dump(page, job, f"page{page_no}_no_next")
            status = "failed:no_next_button"
            log.append({"page": page_no, **result, "action": "no_next_button"})
            break

        advanced = False
        for attempt in range(1, MAX_NEXT_RETRIES + 1):
            nxt.first.click()
            _pause(1.5, 2.5)
            if _has_errors(dialog):
                input(f"   Page {page_no} was not accepted. Fix the highlighted fields in the browser, "
                      "then press Enter to save this page and continue... ")
                continue
            if _page_signature(dialog) != signature or _submit_button(dialog).count():
                advanced = True
                break
            _pause(1.5, 2.5)  # page may still be loading; check once more
            if _page_signature(dialog) != signature:
                advanced = True
                break
        log.append({"page": page_no, "title": signature.split("|")[0], **result,
                    "action": "saved_and_next" if advanced else "not_accepted"})
        if not advanced:
            status = f"failed:page_{page_no}_not_accepted"
            break
        _write_log(job, log, f"in_progress_page_{page_no + 1}")  # progress survives a crash

    saved = False
    if status != "applied":
        saved = _close(page, save_draft=True)
    _write_log(job, log, status, saved)
    if saved:
        print("   partial application saved as a LinkedIn draft")
    return status


def run_applications(jobs: list[dict], answerer, resume: str, auto_submit: bool) -> dict:
    """Returns {job_id: status}. Status: applied | external | skipped | failed:<why>."""
    results = {}
    resume = str(Path(resume).resolve())
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/feed/")
        if "login" in page.url or "authwall" in page.url or "checkpoint" in page.url:
            input("Log in to LinkedIn in the browser window, then press Enter here... ")

        for job in jobs:
            print(f"\n-> {job['title']} @ {job['company']}")
            try:
                page.goto(job["url"])
                _pause(2.5, 4.5)
                answerer.set_job(job)
                results[job["id"]] = _apply_one(page, job, answerer, resume, auto_submit)
                print(f"   {results[job['id']]}")
            except Exception as e:
                results[job["id"]] = f"failed:{type(e).__name__}"
                print(f"   failed: {e}")
                _close(page, save_draft=True)
            _pause(8, 20)  # human-like gap between applications
        ctx.close()
    return results
