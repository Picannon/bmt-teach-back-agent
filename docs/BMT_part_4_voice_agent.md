# BMT Part 4 — Live Voice Agent (ambient class capture)

**Goal:** replace the static `voice-transcript.txt` with a **live voice agent**
that listens to the outpatient class, but only when it should — it stays asleep
until an *armed day*, and even then only *captures* once it hears trigger
keywords (`class`, `transplant`, `BMT`, `tacrolimus`, `CellCept`…). When the
conversation ends it writes a real transcript and feeds it into the pipeline we
already have. Nothing downstream changes: `generate_questions.py` →
`questions.json` → `teach_back_agent.py` still runs exactly as today.

This is a 1-week plan. Decisions already made:
- **STT:** Deepgram streaming (real ambient capture + server-side keyword spotting).
- **Memory/DB:** SQLite behind a thin `memory.py` (Supabase is a later upgrade, see below).
- **Phone testing:** PWA over a Cloudflare Tunnel — same mechanism Part 1 already uses. No Expo.
- **First deliverable:** this plan doc, then build voice agent → phone → memory.

---

## Where the voice agent slots in

```
                  ┌─────────────────────────  voice_agent.py (NEW)  ──────────────────────────┐
   phone mic ──►  │  arm gate (armed day?)  →  Deepgram WS stream  →  keyword wake  →  capture │
   (browser WS)   │                                                        │                   │
                  └────────────────────────────────────────────────────────┼──────────────────┘
                                                                            ▼
                                              transcript  ──►  generate_questions.py  ──►  questions
                                                    │                                          │
                                                    ▼                                          ▼
                                            memory.py (SQLite)  ◄────────  server.py / teach_back_agent.py
                                                    ▲                                          │
                                                    └──────────  phone teach-back PWA  ◄───────┘
```

The **only** change to the existing pipeline: the transcript now arrives from
`voice_agent.py` instead of a checked-in file. `generate_questions.generate_questions(transcript, prompt)`
already takes a transcript *string*, so the handoff is a function call, not a rewrite.

---

## Two containers + a shared store

### 1. `voice_agent.py` — the listener (new, self-contained)
Runs inside the same FastAPI app but as its own router/module. Responsibilities:

- **Arm gate.** A patient/class has an `armed_at` datetime in the DB. Before that
  time the stream endpoint accepts the socket but stays *idle* (no Deepgram, no
  capture) and reports `state: "asleep"`. This is "it begins to listen on a
  certain day." Manual override for the demo: `POST /api/voice/arm`.
- **Stream proxy.** Browser captures mic audio and streams it over a WebSocket to
  `/api/voice/stream`. The server proxies the audio to **Deepgram's streaming
  API** (key stays server-side, never in the browser) and receives interim +
  final transcripts back.
- **Keyword wake.** The agent starts in `listening` (cheap, transcribing but
  discarding). When a **final** transcript contains a trigger keyword, it flips to
  `capturing` and starts appending finals to the session transcript. After a
  configurable silence window with no on-topic speech, it flips back to
  `listening`. Deepgram keyword boosting improves recall of the medical terms.
- **Handoff.** On explicit stop (or long silence) it: persists the transcript via
  `memory.save_transcript(...)`, calls `generate_questions()` on it, saves the
  question set via `memory.save_questions(...)`, and returns the new
  `transcript_id` / `question ids`. That is the moment the teach-back becomes
  available on the phone.

**State machine:** `asleep → listening → capturing → listening → … → done`.
Emit each transition back over the socket so the phone UI can show a live status
pill ("💤 asleep" / "👂 listening" / "🔴 capturing").

### 2. Teach-back side (already built — leave the brains alone)
`generate_questions.py`, `teach_back_agent.py` (`SYSTEM`/`TOOLS`), and the
resumable driver in `server.py` stay as-is. They only change *where they read/write*
state: from JSON files → `memory.py`. The agent loop is untouched.

### 3. `memory.py` — one SQLite store both sides use (new)
Replaces `sessions/*.json`, `care_team_followup.jsonl`, the on-disk
`questions.json`, and the in-memory `_conversations` dict (which currently dies on
restart — a demo risk). Single file `bmt.db`, no server, `sqlite3` from stdlib.

Suggested schema (thin, clinical-auditable — no vector store, on purpose):

```
patients(id, name, armed_at, created_at)
transcripts(id, patient_id, label, text, source['live'|'synthetic'], created_at)
question_sets(id, transcript_id, patient_id, questions_json, created_at)
teachback_sessions(id, patient_id, phase, covered_json, needs_followup_json, created_at)
escalations(id, patient_id, phase, topic, reason, created_at)
conversations(id, patient_id, phase, messages_json, pending_json, finished, created_at)
```

`memory.py` API mirrors the helpers that already exist so the call sites barely change:
- `load_prior_misses(patient_id)` — now a `teachback_sessions` query (same shape returned).
- `save_session(patient_id, record)` — insert into `teachback_sessions`.
- `write_escalation(entry)` — insert into `escalations` (replaces the `.jsonl` append).
- `save_transcript(...)`, `save_questions(...)`, `get_questions(...)` — new, for the voice handoff.
- `save_conversation(cid, conv)` / `load_conversation(cid)` — persist the agent transcript so a restart mid-demo doesn't drop the session.

**Supabase upgrade path (do NOT do week one):** keep every DB call inside
`memory.py`. If we later want the phone to update *live* the instant the voice
agent finishes (instead of the current 0.5s `/api/notifications` poll), swap the
SQLite body of `memory.py` for the Supabase client and subscribe the phone to the
`question_sets` table. One file changes; nothing else.

---

## Phone testing (no Expo — reuse the Part 1 mechanism)

1. `./.venv/bin/uvicorn server:app --reload` on the laptop.
2. `cloudflared tunnel --url http://localhost:8000` → prints an `https://…trycloudflare.com` URL.
3. Open that URL on the phone → **Add to Home Screen** → app-like icon.
4. HTTPS is what unlocks **`getUserMedia` (mic)** + the WebSocket on iOS Safari —
   plain `http://LAN-ip` will not grant the mic. The tunnel gives us that for free.

**Why this beats Expo for a 1-week demo:** the app is already a mobile web app;
Expo means rebuilding the UI in a native toolchain and managing an app-store/dev
build for zero demo benefit. Expo only earns its cost if we need true *background*
always-on listening — we don't; the demo listens with the page open.

**Proving "the voice agent works" on the phone (the milestone you asked for):**
open the PWA, tap **Start listening** → status shows `👂 listening` → say a
sentence *without* a keyword (nothing captured) → say "let's talk about your
**transplant** class and **tacrolimus**" → status flips to `🔴 capturing` and the
live transcript starts filling → tap **Stop** → within a few seconds the
generated teach-back questions appear. That single flow is the whole thesis on a
phone.

---

## Endpoints (added to the existing FastAPI app)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/voice/arm` | Manually arm a patient now (demo override for "the day"). |
| `WS`   | `/api/voice/stream` | Phone streams mic audio; server returns state + live transcript. |
| `POST` | `/api/voice/stop` | End capture → persist transcript → `generate_questions` → return ids. |
| `GET`  | `/api/voice/status` | Current state + which patient is armed. |

Existing `/api/questions/{id}`, `/api/notify`, `/api/notifications`, `/api/answer`
are unchanged — they just read from `memory.py` now.

---

## Day-by-day (1 week)

**Day 1 — Deepgram + prove capture on the laptop.**
Add `DEEPGRAM_API_KEY` to `.env(.example)`. Write a minimal `voice_agent.py` that
opens a Deepgram streaming socket and prints finals. Feed it laptop-mic audio.
Success = live transcript in the terminal. (No keywords/DB yet.)

**Day 2 — voice on the phone.**
Add the mic-capture WS to the frontend + a `/voice.html` page with Start/Stop and
a status pill. Wire the tunnel. Success = **live transcript on the phone.** This is
the riskiest bit (iOS mic + WS over the tunnel) — de-risk it early.

**Day 3 — arm gate + keyword wake + handoff.**
Add the `asleep → listening → capturing` state machine and the trigger-keyword
list. On stop, call `generate_questions()` and log the result. Success = say the
keyword phrase → questions get generated from what was actually said.

**Day 4 — `memory.py` (SQLite) + migrate.**
Create the schema and port `load_prior_misses` / `save_session` / escalations /
conversations off JSON onto it. Persist `save_transcript` / `save_questions`.
Success = restart the server mid-session and nothing is lost; a live class →
questions → teach-back → escalation all land in `bmt.db`.

**Day 5 — end-to-end on the phone.**
Full loop on the device: listen → capture live class → generate questions →
notification → teach-back conversation → an escalation row written. This is the
demo take.

**Day 6 — hardening + fallbacks.**
Silence/endpointing tuning, keyword boosting, reconnect on dropped WS, and a
**"load synthetic transcript" fallback button** (uses the old `voice-transcript.txt`)
so the live demo can't hard-fail on venue Wi-Fi/mic. Record a backup screen capture.

**Day 7 — buffer / polish / script the 3-min demo.**

---

## Verification checklist
- [ ] Laptop: Deepgram finals print from mic audio.
- [ ] Phone: live transcript renders over the tunnel (mic permission granted on HTTPS).
- [ ] Non-keyword speech is **not** captured; keyword speech flips to `capturing`.
- [ ] Before `armed_at`, the agent reports `asleep` and captures nothing.
- [ ] Stop → a real `questions.json`-shaped set is generated from the *spoken* words.
- [ ] Server restart mid-session loses nothing (SQLite).
- [ ] A missed red flag still writes an `escalations` row.
- [ ] Synthetic-transcript fallback button works if live capture fails.

## Explicitly out of scope (say the one-liner if asked)
- True OS background / locked-screen always-listening (needs native; deferred).
- Real Abridge API (not available for this build — Deepgram + Web mic stand in).
- Multi-patient concurrency, auth, real PHI. Synthetic/mock data only.
