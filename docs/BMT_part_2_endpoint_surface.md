# BMT Part 2 — Endpoint Surface

The full notification → question → answer → evaluation flow, defined as APIs.
Notifications only fire while the app is **open** on the phone (confirmed scope).

## Full flow

```
laptop (notify.py)                server (laptop)                phone
   │  POST /api/notify {question_id}    │                          │
   │ ─────────────────────────────────▶│  enqueue notification    │
   │                                    │                          │
   │                                    │◀── GET /api/notifications  (poll every 0.5s)
   │                                    │ ───▶ new notification     │
   │                                    │                     banner slides in
   │                                    │                     tap → /question.html?id=N
   │                                    │◀── GET /api/questions/{id}
   │                                    │ ───▶ question            │
   │                                    │                     patient types answer, Submit
   │                                    │◀── POST /api/answer {question_id, answer}
   │                                    │   look up question,       │
   │                                    │   call evaluation.py      │
   │                                    │ ───▶ {question_id, answer, evaluation}
   │                                    │                     show agent's response
```

## Pages (2)

| Page | Route | Does |
|------|-------|------|
| **Home** | `/` | Generic screen; polls `GET /api/notifications` every **0.5s**; pops the banner; tap → question page |
| **Question** | `/question.html?id=N` | Fetches the question, shows it, gives a text box + Submit, then renders the agent's evaluation |

## Data model

- `questions.json` is the source of truth. Generate once:
  `python generate_questions.py --out questions.json`.
- **`generate_questions.py` bakes an `id` (= list index) into each question**
  when it writes the file. Ids live in the data, not attached at serve time.
- Notifications live in a simple in-memory list on the server (single uvicorn
  process — fine for the demo).

## Endpoints

### `POST /api/notify`  — laptop triggers a notification
Sender (`notify.py`) picks the question; server just relays it.
```jsonc
// request
{ "question_id": 3, "title": "Check-in", "body": "Quick question about CellCept" }
// title/body optional — server fills defaults from the question if omitted
// response
{ "id": 7, "question_id": 3, "title": "Check-in", "body": "…", "created_at": "…" }
```

### `GET /api/notifications`  — phone polls (every 0.5s)
Consume-on-read: returns any pending notifications and clears the queue, so the
phone shows each one once with no client-side id tracking. (We assume the server
side won't re-send needlessly; re-sending the same question just shows it again.)
```jsonc
// response
{ "notifications": [
    { "id": 7, "question_id": 3, "title": "Check-in", "body": "…", "created_at": "…" }
] }
```

### `GET /api/questions/{id}`  — question page fetches one question
The only questions endpoint (no "all questions" route — nothing needs it).
```jsonc
// response  (patient-facing fields; see note below)
{ "id": 3, "topic": "cellcept dosing", "question": "How often do you take CellCept?",
  "source_quote": "CellCept is usually taken every 8 hours.", "care_phase": "both" }
```
> **Note:** we omit `expected_answer` from this response so the answer key isn't
> exposed to the client — grading happens server-side. (Trivial to include if we
> decide we want it for the demo.)

### `POST /api/answer`  — patient submits an answer, agent evaluates
The extendible core. Server loads the question by id, calls `evaluation.py`, returns the triple.
```jsonc
// request
{ "question_id": 3, "answer": "I take it twice a day" }
// response
{ "question_id": 3,
  "answer": "I take it twice a day",
  "evaluation": { "verdict": "incorrect", "feedback": "CellCept is every 8 hours (3×/day)…" } }
```
**Future multi-turn** (not built now): keep the same endpoint and `question_id`,
but replace the single `answer`/`evaluation` with a list:
```jsonc
{ "question_id": 3, "turns": [
    { "answer": "twice a day", "evaluation": { "verdict": "incorrect", "feedback": "…" } },
    { "answer": "oh, every 8 hours", "evaluation": { "verdict": "correct", "feedback": "…" } }
] }
```

## Evaluation hook — `evaluation.py` (your friend owns this; I won't touch it)

`POST /api/answer` calls into it. Agreed contract:
```python
def evaluate_answer(question: dict, answer: str) -> dict:
    """question = one item from questions.json (full object, incl. expected_answer);
    answer = the patient's text.  Returns e.g. {"verdict": ..., "feedback": ...}."""
```
The endpoint looks up the full question object, calls `evaluate_answer(question,
answer)`, and returns its dict under the `evaluation` key. It imports defensively:
until `evaluation.py` exists with that function, the endpoint returns a
**placeholder** `{"verdict": "placeholder", "feedback": "success!"}` so the flow
is testable now. A real `evaluate_answer` that raises is surfaced as a JSON error.

## Build steps

1. **`generate_questions.py`:** bake `id` (= index) into each question on write.
2. **Server:** `GET /api/questions/{id}`, `POST /api/notify`,
   `GET /api/notifications`, `POST /api/answer` (+ placeholder evaluation).
3. **Home (`app.js`):** 0.5s poll loop + banner + tap→navigate (+ optional real OS notification).
4. **Question page (`question.html` + js):** fetch by id, answer box + Submit, render evaluation.
5. **Laptop (`notify.py`):** trigger CLI (`--id` / `--topic` / `--random`).
6. **Test:** locally, then over the tunnel to the phone.

## Deferred to Part 3

- Multi-turn conversation (the `turns` list above) with history.
- Real background / locked-screen push (Web Push + service worker).