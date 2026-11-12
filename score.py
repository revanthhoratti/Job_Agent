from llm import ask_json

SYSTEM = (
    "You are a strict, honest recruiter screening one job against one candidate. "
    "Score only on evidence in the profile. Reply with JSON only."
)

PROMPT = """CANDIDATE PROFILE:
{profile}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Return JSON with keys:
  "score": integer 0-100 (fit for this candidate; penalize heavily if the job needs
           far more experience than ~3 years of GenAI/ML, or is not really an AI/LLM/agent role),
  "role_type": "genai" | "agentic" | "ml" | "other",
  "matched": list of up to 6 candidate strengths relevant to this job,
  "gaps": list of up to 5 requirements the candidate lacks,
  "reason": one sentence
"""


def score_job(job: dict, description: str, profile: str, model: str) -> dict:
    return ask_json(
        PROMPT.format(
            profile=profile,
            title=job["title"],
            company=job["company"],
            location=job["location"],
            description=description or "(description unavailable)",
        ),
        SYSTEM,
        model,
    )
