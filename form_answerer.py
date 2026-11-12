"""Answer job-application questions with the LLM, using only the candidate's own facts.

Instead of matching keywords, the model reads each question (plus its dropdown / radio
options) and answers from profile.md and answers.yaml. It never invents facts:

- Yes/No and "years of experience" questions are answered from the dated roles in the profile.
- Salary, notice period, relocation, visa/work authorization, demographics: answered ONLY if the
  value is given in answers.yaml. Otherwise you are asked.
- Anything the model is not confident about, any free-text answer (optional), and anything it
  cannot support from your profile is shown to you in the terminal first:
      Enter = accept the suggestion, type your own answer, or '-' to leave it blank.
- Answers you confirm or type are remembered in answer_cache.json and reused next time.
"""
import datetime
import difflib
import json
import re
from pathlib import Path

from llm import ask_json

ROOT = Path(__file__).parent
CACHE_FILE = ROOT / "answer_cache.json"

PLACEHOLDERS = {"select an option", "select", "please select", "choose", "--", "-", ""}

SENSITIVE = re.compile(
    r"salary|ctc|compensation|notice period|gender|race|ethnic|disab|veteran|religio|sexual|caste|"
    r"relocat|sponsor|visa|work authori|authorized to work|authorised to work|legally|criminal|convict|"
    r"date of birth|marital|nationality|passport|background check|drug|pronoun",
    re.I,
)

PROMPT = """You are filling in a job application form on behalf of the candidate. Use ONLY the candidate facts and profile below.
Never invent employers, skills, degrees, numbers, salaries or dates. If the information is not there, set "answer" to "" and "source" to "unknown".

Today's date: {today}

CANDIDATE FACTS (key: value):
{facts}

CANDIDATE PROFILE:
{profile}

JOB APPLIED FOR: {title} at {company}
JOB DESCRIPTION (excerpt):
{jd}

FORM QUESTION: {question}
FIELD TYPE: {kind}
{options_block}
Rules:
- select/radio: "answer" must be copied exactly from the OPTIONS list.
- number: "answer" is digits only (for example 3 or 2.5).
- Yes/No questions about a skill or tool: "Yes" if the profile lists or clearly implies it, "No" if it is absent from the profile.
- "Years of experience with X": work it out from the dated roles in the profile and today's date, rounded down to a whole number. LLM / GenAI / RAG work started in Jan 2023. If X does not appear in the profile at all, answer 0 with confidence "low".
- Notice period, salary / CTC, relocation, sponsorship, work authorization and demographic questions: answer only if the value is in CANDIDATE FACTS (and not "not provided"); otherwise unknown.
- textarea: first person, at most 100 words, concrete, based only on the profile and the job description.
- "source": "facts" if taken from CANDIDATE FACTS, "profile" if stated in the profile, "inferred" if you calculated or deduced it, "unknown" if unsupported.
- "confidence": "high" only if directly stated; "medium" if inferred; "low" if you are guessing.

Reply with JSON only: {{"answer": "...", "confidence": "high|medium|low", "source": "facts|profile|inferred|unknown", "reason": "one short sentence"}}
"""


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def match_option(answer: str, options: list[str]):
    """Map an answer onto one of the real options (exact, then close match). None if no match."""
    if not options:
        return answer
    a = _norm(answer)
    by_norm = {_norm(o): o for o in options}
    if a in by_norm:
        return by_norm[a]
    for n, o in by_norm.items():
        if a and (n.startswith(a) or a.startswith(n)):
            return o
    close = difflib.get_close_matches(a, list(by_norm), n=1, cutoff=0.7)
    return by_norm[close[0]] if close else None


class Answerer:
    def __init__(self, profile: str, facts: dict, cfg: dict):
        self.profile = profile
        self.model = cfg["model"]
        self.confirm_free_text = cfg.get("confirm_free_text", True)
        self.ask_below = cfg.get("answer_ask_below", "high")  # "high": ask unless directly stated
        self.job: dict = {}
        self.facts_text = "\n".join(
            f"- {k}: {'(not provided)' if str(v).strip().upper() in ('', 'TODO', 'NONE') else v}"
            for k, v in facts.items()
        )
        self.cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

    def set_job(self, job: dict):
        self.job = job

    # ------------------------------------------------------------------ public
    def __call__(self, question: str, kind: str, options: list[str] | None = None):
        """Return {'answer', 'source', 'confidence', 'user_confirmed'} or None to leave blank."""
        question = " ".join(question.split())
        options = [o for o in (options or []) if o.strip().lower() not in PLACEHOLDERS]
        key = f"{kind}::{_norm(question)}::{'|'.join(sorted(_norm(o) for o in options))}"

        if kind != "textarea" and key in self.cache:
            return {"answer": self.cache[key]["answer"], "source": "saved", "confidence": "high",
                    "user_confirmed": True}

        try:
            r = self._ask_model(question, kind, options)
        except Exception as e:  # SystemExit (Ollama down) is not caught on purpose
            r = {"answer": "", "confidence": "low", "source": "unknown", "reason": f"model error: {e}"}

        answer = self._clean(str(r.get("answer", "")), kind, options)
        conf = str(r.get("confidence", "low")).lower()
        source = str(r.get("source", "unknown")).lower()
        reason = str(r.get("reason", ""))

        needs_user = (
            not answer
            or source == "unknown"
            or conf == "low"
            or (SENSITIVE.search(question) and source != "facts")
            or (kind == "textarea" and self.confirm_free_text)
            or (self.ask_below == "high" and conf != "high")
        )
        if needs_user:
            if SENSITIVE.search(question) and source != "facts":
                answer = ""  # never offer a guess for salary, notice period, demographics, etc.
            final = self._ask_user(question, kind, options, answer, conf, reason)
            if final is None:
                return None
            if kind != "textarea":
                self.cache[key] = {"question": question, "answer": final}
                CACHE_FILE.write_text(json.dumps(self.cache, indent=2))
            return {"answer": final, "source": "you", "confidence": "high", "user_confirmed": True}
        return {"answer": answer, "source": source, "confidence": conf, "user_confirmed": False}

    # ----------------------------------------------------------------- helpers
    def _ask_model(self, question, kind, options):
        options_block = ("OPTIONS (choose exactly one): " + json.dumps(options)) if options else ""
        return ask_json(
            PROMPT.format(
                today=datetime.date.today().isoformat(),
                facts=self.facts_text,
                profile=self.profile,
                title=self.job.get("title", ""),
                company=self.job.get("company", ""),
                jd=(self.job.get("description") or "(not available)")[:2500],
                question=question,
                kind=kind,
                options_block=options_block,
            ),
            "You fill job application forms accurately and honestly. Reply with JSON only.",
            self.model,
            max_tokens=500,
        )

    @staticmethod
    def _clean(answer: str, kind: str, options: list[str]) -> str:
        a = answer.strip()
        if not a or a.lower() in ("unknown", "n/a", "null"):
            return ""
        if kind in ("select", "radio") and options:
            return match_option(a, options) or ""
        if kind == "number":
            m = re.search(r"\d+(\.\d+)?", a)
            return m.group(0) if m else ""
        return a

    @staticmethod
    def _ask_user(question, kind, options, suggestion, conf, reason):
        print(f"\n   ? {question}")
        if options:
            print("     options: " + " | ".join(options))
        if suggestion:
            print(f"     suggested ({conf}): {suggestion!r}" + (f"   [{reason}]" if reason else ""))
            prompt = "     Enter = accept, type your own answer, or '-' to leave blank: "
        else:
            print("     I can't answer this from your profile.")
            prompt = "     Type an answer, or press Enter to leave it blank: "
        typed = input(prompt).strip()
        if typed == "-" or (not typed and not suggestion):
            return None
        answer = typed or suggestion
        if kind in ("select", "radio") and options:
            matched = match_option(answer, options)
            if not matched:
                print("     that isn't one of the options, leaving it blank")
                return None
            return matched
        return answer
