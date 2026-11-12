from pathlib import Path

from llm import ask

SYSTEM = (
    "You write concise, specific job application material for the candidate. "
    "Use ONLY facts present in the candidate profile. Never invent employers, metrics, "
    "tools, or years of experience. If the job asks for something the candidate lacks, "
    "do not claim it."
)

PROMPT = """CANDIDATE PROFILE:
{profile}

JOB: {title} at {company} ({location})
{description}

Known fit notes: matched={matched}; gaps={gaps}

Produce markdown with exactly these sections:
## Cover note
Max 120 words, first person, no clichés, mention 2 concrete things from the profile that match.
## Resume tweaks
3-5 bullets: which existing resume points to move up or reword for this role (no new claims).
## Recruiter message
Max 60 words, suitable as a LinkedIn message.
"""


def make_packet(job: dict, description: str, profile: str, model: str, out_root: Path) -> Path:
    body = ask(
        PROMPT.format(
            profile=profile,
            title=job["title"],
            company=job["company"],
            location=job["location"],
            description=description[:5000],
            matched=job.get("matched"),
            gaps=job.get("gaps"),
        ),
        SYSTEM,
        model,
        max_tokens=900,
    )
    folder = out_root / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "packet.md"
    path.write_text(
        f"# {job['title']} - {job['company']}\n{job['url']}\n"
        f"Score: {job.get('score')} | {job.get('reason', '')}\n\n{body}\n"
    )
    return path
