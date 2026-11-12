"""Find jobs through LinkedIn's public (logged-out) job search endpoints.

No login needed for discovery. These endpoints are unofficial and can change
or rate-limit; the code backs off and stops quietly if blocked.
"""
import random
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def search(keywords: str, location: str, hours: int, limit: int) -> list[dict]:
    jobs, start = [], 0
    while len(jobs) < limit:
        r = requests.get(
            SEARCH_URL,
            params={
                "keywords": keywords,
                "location": location,
                "f_TPR": f"r{hours * 3600}",
                "start": start,
            },
            headers=HEADERS,
            timeout=20,
        )
        if r.status_code != 200:
            print(f"  search stopped (HTTP {r.status_code}) for '{keywords}' in {location}")
            break
        cards = BeautifulSoup(r.text, "html.parser").select("li")
        if not cards:
            break
        for c in cards:
            div = c.select_one("div.base-card")
            if not div:
                continue
            job_id = div.get("data-entity-urn", "").split(":")[-1]
            if not job_id.isdigit():
                continue
            jobs.append(
                {
                    "id": job_id,
                    "title": _text(c.select_one(".base-search-card__title")),
                    "company": _text(c.select_one(".base-search-card__subtitle")),
                    "location": _text(c.select_one(".job-search-card__location")),
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                }
            )
        start += len(cards)
        time.sleep(random.uniform(2, 4))
    return jobs[:limit]


def fetch_description(job_id: str) -> str:
    r = requests.get(DETAIL_URL.format(job_id), headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    return _text(soup.select_one(".show-more-less-html__markup"))[:8000]
