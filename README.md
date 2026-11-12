# Job application agent

Pipeline: **discover** (LinkedIn public search) -> **score** (Claude vs `profile.md`) ->
**tailor** (cover note + resume tweaks in `out/<job_id>/packet.md`) -> **apply**
(Playwright fills Easy Apply forms in your own logged-in browser).

## Setup
```bash
cd job_agent
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...      # Windows PowerShell: $env:ANTHROPIC_API_KEY="..."
cp /path/to/your/resume.pdf resume.pdf
```
Then edit `answers.yaml` (replace every TODO) and `config.yaml` (searches, locations, min_score).

## Use
```bash
python main.py run        # find, score, and prepare packets
python main.py status     # see the ranked list
python main.py apply      # opens a browser; log in once, then it works through the queue
```

## Safety rails (on purpose)
- `auto_submit: false`: you confirm each submission. Set `true` only after you have watched it
  handle several applications correctly.
- Daily cap (`max_applications_per_day`), random delays, one job at a time.
- Unknown answers are never guessed; it pauses and lets you fill them in.
- Only Easy Apply roles are automated; external links are marked `external` for you to do by hand.
- Cover notes only use facts in `profile.md`. Keep that file accurate.

## Know the risks
LinkedIn's terms prohibit automated activity on the site, and accounts that apply in bulk can be
restricted. Discovery uses unofficial public endpoints that may change. Selectors for the Easy Apply
dialog can break when LinkedIn updates its UI. Check `out/` packets before sending anything extra
(recruiter messages are drafted, never sent automatically).
