# Project Context: BMT Knowledge Continuity Agent
### Abridge x Anthropic Hackathon — build in progress

## Who's building this
Piccanon — BMT/oncology charge nurse, domain expert, providing all clinical accuracy. Has a technical teammate (voice agent + health informatics background) helping build. Competing as a team, not solo.

## The reframe / pitch
Everyone else solving this space targets the discharge end. The actual failure point is the unverified handoff between outpatient teaching and inpatient reality — patients get outpatient BMT education classes before admission, but inpatient staff assume they retained everything, and there's no check on that assumption. Readmissions often trace back to GVHD (graft-versus-host disease — donor cells attacking the patient's body), tied to medication non-adherence (tacrolimus, Bactrim) or missed education (e.g. lung GVHD / fungal infection risk and when to wear a mask based on air quality).

Submitted description: *"BMT Knowledge Continuity Agent — an AI agent using Abridge's clinical conversation capture to reconcile outpatient BMT education (classes, after-visit summaries) against a knowledge check at admission and a personalized teach-back at discharge to reduce GVHD readmissions."*

## Locked scope — Pipeline 1 (primary demo, must be finished)
Synthetic discharge note + synthetic outpatient education record as input →
- **Agent 1 (reconciliation):** flags what's new vs. already covered in outpatient teaching
- **Agent 2 (generation):** produces plain-English explanation + personalized red-flag list + teach-back questions
- Output shows generic instructions vs. personalized version side by side

## Pipeline 2 — admission-side teach-back (secondary, build as time allows)
Originally scoped as verbal-only ("what's next" in Q&A), now partially built because time allowed. Real Abridge API access is **not available** for this build — voice capture uses the standard browser Web Speech API instead.

- **Capture — DONE.** Synthetic class transcripts stand in for the ambient-listened class: `class_A_tacrolimus.md`, `class_B_cellcept.md` (in `voice-agent/`). No further build needed here.
- **Turn-loop agent — HIGHEST PRIORITY, ~90% done.** One Claude call per turn, combining question generation + evaluation + remediation into a single system prompt (`voice-agent/server.js`, `/api/teach-back`). Grounded in the class transcripts loaded from disk. **Immediate next step:** edit the system prompt so that on a wrong answer, it quotes the relevant transcript line verbatim before explaining — right now it paraphrases from memory instead of citing the source content.
- **Summary agent — optional, build only if time remains.** One more Claude call at session end (`/api/summary`, not yet built) sending full conversation history, producing what was covered / missed / needs follow-up.

## What's already built
- `voice-agent/server.js` — Express backend, proxies to Claude Haiku 4.5, keeps API key server-side, loads class transcripts from disk as grounding context
- `voice-agent/public/index.html` — frontend using native Web Speech API (SpeechRecognition + SpeechSynthesis) for STT/TTS, no third-party voice service
- `voice-agent/package.json`, `voice-agent/.env.example`
- `class_A_tacrolimus.md`, `class_B_cellcept.md` — synthetic outpatient class transcripts (tacrolimus + CellCept), also exist as combined and short versions

## Two drugs in scope
Tacrolimus (FK506) and CellCept (mycophenolate mofetil) — both anti-rejection/GVHD-prevention meds. Framed in the pitch as generalizable to other drugs/chemo later.

## Rules being held to
- Public GitHub repo, early/frequent commits as timestamp proof
- Synthetic/mocked data only — no real patient data, even de-identified
- README's first commit is a "what I'm building and why" statement before any code
- Be ready to verbally separate "built today" vs. domain knowledge brought in
- Model choice for voice loop: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) for turn latency — don't upgrade to Sonnet/Opus for this loop unless quality, not speed, is the bottleneck

## Demo plan
3 minutes + Q&A. Open with the reframe. Show the discharge-reconciliation pipeline (Pipeline 1) working live. Name its Monday-usability explicitly. If Pipeline 2's teach-back loop is working, demo it live too; if not, have a one-liner ready for that question.
