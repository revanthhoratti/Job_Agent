"""Job application agent.

  python main.py discover   find new jobs
  python main.py score      score them against profile.md
  python main.py tailor     write a cover note / resume tweaks per good match
  python main.py apply      apply on LinkedIn (Easy Apply) and Naukri (you stay in control)
  add  --source naukri  (or linkedin)  to any of discover / score / apply to use one site only
  python main.py run        discover + score + tailor (then run `apply` when ready)
  python main.py status     show the pipeline
"""
import argparse
import datetime
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DB = ROOT / "jobs.json"


def cfg():
    import llm

    c = yaml.safe_load((ROOT / "config.yaml").read_text())
    llm.configure(c)
    return c


def load():
    return json.loads(DB.read_text()) if DB.exists() else {}


def save(db):
    DB.write_text(json.dumps(db, indent=2))


def sources_for(c, only=None):
    wanted = [only] if only else c.get("sources", ["linkedin"])
    return [s for s in wanted if s in ("linkedin", "naukri")]


def cmd_discover(c, only=None):
    db, new = load(), 0
    blocked = [b.lower() for b in c.get("blocklist_companies", [])]

    def keep(j):
        return (
            j["id"] not in db
            and not any(b in j["company"].lower() for b in blocked)
            and any(k in j["title"].lower() for k in c["title_must_include_any"])
        )

    srcs = sources_for(c, only)
    if "linkedin" in srcs:
        from discover import search

        for s in c["searches"]:
            for loc in c["locations"]:
                print(f"Searching LinkedIn: '{s['keywords']}' in {loc}")
                for j in search(s["keywords"], loc, c["posted_within_hours"], c["max_results_per_search"]):
                    if keep(j):
                        j.update(source="linkedin", status="discovered")
                        db[j["id"]] = j
                        new += 1
        save(db)

    if "naukri" in srcs:
        import naukri

        jobs = naukri.discover(
            [s["keywords"] for s in c["searches"]],
            c.get("naukri_locations", ["Bengaluru", "India"]),
            max(1, c["posted_within_hours"] // 24),
            c["max_results_per_search"],
            lambda j: keep(j) and j["id"] not in db,
        )
        for j in jobs:
            j["status"] = "discovered"
            db[j["id"]] = j
            new += 1
        save(db)
    print(f"{new} new jobs added ({len(db)} total)")


def cmd_score(c, only=None):
    from discover import fetch_description
    from score import score_job

    profile = (ROOT / "profile.md").read_text()
    db = load()
    for j in [x for x in db.values() if x["status"] == "discovered" and (not only or x.get("source", "linkedin") == only)]:
        # Naukri descriptions were read during discovery; LinkedIn's are fetched here.
        desc = j.get("description") or (fetch_description(j["id"]) if j.get("source", "linkedin") == "linkedin" else "")
        try:
            r = score_job(j, desc, profile, c["model"])
        except Exception as e:
            print(f"  score failed for {j['title']}: {e}")
            continue
        j.update(
            score=int(r.get("score", 0)),
            role_type=r.get("role_type"),
            matched=r.get("matched"),
            gaps=r.get("gaps"),
            reason=r.get("reason"),
            description=desc,
        )
        j["status"] = "scored" if j["score"] >= c["min_score"] else "low_fit"
        print(f"  {j['score']:>3}  {j['title']} @ {j['company']}")
        save(db)


def cmd_tailor(c):
    from tailor import make_packet

    profile = (ROOT / "profile.md").read_text()
    db = load()
    for j in [x for x in db.values() if x["status"] == "scored"]:
        path = make_packet(j, j.get("description", ""), profile, c["model"], ROOT / "out")
        j["status"] = "ready"
        print(f"  packet: {path}")
        save(db)


def cmd_apply(c, only=None):
    from form_answerer import Answerer

    db = load()
    today = datetime.date.today().isoformat()
    srcs = sources_for(c, only)
    done_today = sum(1 for j in db.values() if j.get("applied_on") == today)
    room = max(0, c["max_applications_per_day"] - done_today)
    ready = [j for j in db.values() if j["status"] == "ready" and j.get("source", "linkedin") in srcs]
    queue = sorted(ready, key=lambda j: -j["score"])[:room]
    if not queue:
        print("Nothing to apply to (no ready jobs, or daily cap reached).")
        return
    answers = yaml.safe_load((ROOT / "answers.yaml").read_text())
    resume = ROOT / c["resume_pdf"]
    if not resume.exists():
        raise SystemExit(f"Put your resume at {resume}")
    answerer = Answerer((ROOT / "profile.md").read_text(), answers, c)

    results = {}
    for src in srcs:
        batch = [j for j in queue if j.get("source", "linkedin") == src]
        if not batch:
            continue
        print(f"\n=== Applying on {src.title()} ({len(batch)} jobs) ===")
        if src == "linkedin":
            from apply_linkedin import run_applications

            results.update(run_applications(batch, answerer, str(resume), c["auto_submit"]))
        else:
            import naukri

            results.update(naukri.apply_jobs(batch, answerer, c["auto_submit"]))
    for jid, status in results.items():
        db[jid]["status"] = status
        if status == "applied":
            db[jid]["applied_on"] = today
    save(db)


def cmd_status(_):
    db = load()
    counts = {}
    for j in db.values():
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    print(counts)
    for j in sorted(db.values(), key=lambda x: -x.get("score", 0))[:15]:
        print(f"{j.get('score', '-'):>3} {j.get('source', 'linkedin'):<8} {j['status']:<10} {j['title']} @ {j['company']}  {j['url']}")


def cmd_check(c):
    from llm import ask

    print(ask("Reply with the single word: ok", "Reply briefly.", c["model"], max_tokens=10))
    print("API key and model work.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["check", "discover", "score", "tailor", "apply", "run", "status"])
    ap.add_argument("--source", choices=["linkedin", "naukri"], help="limit to one site (default: all in config.yaml)")
    a = ap.parse_args()
    c = cfg()
    steps = {
        "check": [cmd_check],
        "discover": [cmd_discover],
        "score": [cmd_score],
        "tailor": [cmd_tailor],
        "apply": [cmd_apply],
        "run": [cmd_discover, cmd_score, cmd_tailor],
        "status": [cmd_status],
    }[a.command]
    for s in steps:
        if s in (cmd_discover, cmd_score, cmd_apply):
            s(c, a.source)
        else:
            s(c)


if __name__ == "__main__":
    main()
