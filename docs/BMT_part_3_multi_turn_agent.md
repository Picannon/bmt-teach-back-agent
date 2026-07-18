# BMT Part 3 — Multi-turn Agent Conversation

**Goal:** after the notified first question, hand the turn loop to
`teach_back_agent` so it can ask adaptive follow-ups, record notes, escalate
red-flag misses, and finish. The question page becomes a scrollable conversation.

## Reuse from `teach_back_agent.py` (left untouched)

Keeping all four tools, so the agent's brain transfers verbatim:
- **`SYSTEM`, `TOOLS`** (`ask` / `record` / `escalate` / `finish`) — import as-is.
- **`load_prior_misses`, `save_session`, `now_iso`, `FOLLOWUP_FILE`, `SESSIONS_DIR`** — reuse.
- Only `_do_ask` + `run()` are unusable (blocking `input()` / terminal loop). We
  write a **resumable driver** in `server.py` that reuses everything above.

## Server state (in-memory, keyed by `conversation_id`)

No display log stored — the client keeps the visible thread in the DOM (we never
reload the page; to restart, just send another notification). The server only
keeps what it needs to **resume the agent**:

```python
conversations[cid] = {
    "messages": [...],   # raw agent transcript — SERVER ONLY (has answer keys)
    "patient_id", "phase",
    "pending":  {"ask_id", "held_results"} | None,   # the unanswered ask
    "finished": bool,
}
```

**Display events** are returned per-turn in the response (not stored):
| type | rendered as |
|---|---|
| `question` | the question text (+ a fresh answer box when it's the active turn) |
| `answer` | the patient's text, locked (non-editable) |
| `note` | "Note recorded in your clinical document: {note}" |
| `escalate` | "Escalating to your clinician: {topic}" |
| `finish` | "I think you understand! Thanks — I'll check in again later." |

## Resumable driver (`server.py`)

`run_until_ask_or_finish(conv)` — loop `client.messages.create(SYSTEM, TOOLS, messages)`:
- Append the assistant turn to `messages`.
- Walk its `tool_use` blocks, collecting display events + tool results:
  - `record` → `note` event; result `"recorded"`.
  - `escalate` → append to `FOLLOWUP_FILE` + `escalate` event; result `"escalated"`.
  - `finish` → `finish` event, `save_session(...)`, set `finished=True`.
  - `ask` → `question` event, stash `pending` (its `tool_use_id`), **suspend**.
- **Batching rule:** if an `ask` shares the turn with `record`/`escalate`, hold
  their results in `pending.held_results` and suspend (don't send the user turn
  yet). If no `ask`, append the results as a user message and keep looping.
- Returns the list of new events produced this call.

**Resume** (`continue_conversation(conv, answer)`):
- Append one user message = `held_results + [ask tool_result: 'The patient answered: "…"']`.
- Clear `pending`, call `run_until_ask_or_finish`, prepend an `answer` event.

## Endpoints — reuse `/api/answer` (payload grows a `conversation_id`)

- **First submit** (answering the notified Q1):
  `POST /api/answer {question_id, answer}` → create the conversation, seed
  `messages` with a kickoff ("prior misses… available questions… you already
  asked <Q1 text>, patient answered <A1> — record it and continue"), run the
  driver. → `{conversation_id, events, done}`
- **Every submit after:** `POST /api/answer {conversation_id, answer}` → resume.
  → `{conversation_id, events, done}`

Notification pipeline (`notify.py`, `/api/notify`, `/api/notifications`, banner)
is **unchanged**.

## Frontend: `question.html` → conversation page

- **Load:** `GET /api/questions/{id}` → render Q1 + answer box + Submit (turn 1,
  `question_id` known, no `conversation_id` yet).
- **On Submit:** lock the current answer box (readonly, hide its button); POST;
  append the returned events in order (`note` / `escalate` lines, then either a
  new `question` with a fresh answer box + Submit, or a `finish` line and no more
  input). Scrollable thread, auto-scroll to newest.
- Store `conversation_id` from the first response; use it on every later submit.

## Build steps

1. **`server.py`** — `conversations` store, `run_until_ask_or_finish` +
   `continue_conversation`, seed logic, reshape `/api/answer` to branch on
   `question_id` (start) vs `conversation_id` (resume); import the agent bits
   from `teach_back_agent`.
2. **`static/question.js`** — thread rendering + per-turn submit loop; track
   `conversation_id`.
3. **`static/styles.css`** — small styles for `note` / `escalate` / `finish`
   lines + the locked answer.
4. **Test:** notify → answer Q1 wrong (CellCept) → follow-up appears → answer →
   finish; confirm a line landed in `care_team_followup.jsonl`.

## Deferred

- Real background / locked-screen push.