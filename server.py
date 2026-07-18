"""FastAPI server for the BMT teach-back app.

  GET  /                       -> home page (mock iOS home screen)
  GET  /question.html          -> conversation page
  GET  /api/questions/{id}     -> one question (patient-facing; no answer key)
  POST /api/notify             -> laptop enqueues a notification
  GET  /api/notifications      -> phone polls for new notifications
  POST /api/answer             -> drive the multi-turn teach-back agent

The teach-back conversation is driven by teach_back_agent's SYSTEM/TOOLS. That
agent was written as a blocking terminal loop, so we re-drive it here in a
*resumable* form: run until the agent asks the next question (suspend) or
finishes, returning the display events for each turn. The raw transcript
(`messages`, which carries the answer keys) stays server-side.

Run locally:  ./.venv/bin/uvicorn server:app --reload
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import teach_back_agent as agent_mod  # SYSTEM/TOOLS + memory/escalation helpers

QUESTIONS_FILE = Path("questions.json")
PATIENT_ID = "demo-patient"  # single demo patient (enables cross-session memory)
PHASE = "discharge"          # the teach-back happens at discharge
FINISH_TEXT = "I think you understand! Thanks — I'll check in again later."
MAX_AGENT_STEPS = 12         # runaway guard on the tool-use loop

# Web-only brevity cap so the patient doesn't get question fatigue. Added on top
# of teach_back_agent.SYSTEM here (not in that file, so the CLI agent is unaffected).
SYSTEM = agent_mod.SYSTEM + (
    "\n\nIMPORTANT — keep this session SHORT to avoid fatiguing the patient. The "
    "first question has already been asked and answered:\n"
    "- If the patient's first answer was CORRECT, record it and call finish "
    "IMMEDIATELY. Do NOT ask any follow-up question.\n"
    "- If it was WRONG or only PARTIALLY correct, re-teach and ask a follow-up — "
    "but ask AT MOST one or two follow-up questions in total, then call finish.\n"
    "Prefer finishing early over asking another question."
)

app = FastAPI(title="BMT Teach-Back")

# teach_back_agent's import already ran load_dotenv(), so the key is in the env.
_client = anthropic.Anthropic()

# In-memory notification queue + conversation store (single process; demo-fine).
_notifications: list[dict] = []
_next_id = 1
_conversations: dict[str, dict] = {}


def question_by_id(qid: int) -> dict | None:
    questions = json.loads(QUESTIONS_FILE.read_text())["questions"]
    return next((q for q in questions if q.get("id") == qid), None)


class NotifyIn(BaseModel):
    question_id: int
    title: str | None = None
    body: str | None = None


class AnswerIn(BaseModel):
    answer: str
    question_id: int | None = None
    conversation_id: str | None = None


# --- questions + notifications (unchanged) ----------------------------------

@app.get("/api/questions/{qid}")
def api_question(qid: int) -> JSONResponse:
    question = question_by_id(qid)
    if question is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    # Patient-facing: drop the answer key so it isn't exposed to the client.
    patient_view = {k: v for k, v in question.items() if k != "expected_answer"}
    return JSONResponse(patient_view)


@app.post("/api/notify")
def api_notify(payload: NotifyIn) -> JSONResponse:
    global _next_id
    question = question_by_id(payload.question_id)
    if question is None:
        return JSONResponse({"error": "question_not_found"}, status_code=404)
    notification = {
        "id": _next_id,
        "question_id": payload.question_id,
        "title": payload.title or "Teach-back check-in",
        "body": payload.body or question["question"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _notifications.append(notification)
    _next_id += 1
    return JSONResponse(notification)


@app.get("/api/notifications")
def api_notifications() -> JSONResponse:
    # Consume-on-read: hand back pending notifications and clear the queue.
    global _notifications
    pending = _notifications
    _notifications = []
    return JSONResponse({"notifications": pending})


# --- teach-back agent driver ------------------------------------------------

def _tool_result(tool_use_id: str, content: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def _write_escalation(conv: dict, inp: dict) -> None:
    entry = {
        "patient_id": conv["patient_id"],
        "phase": conv["phase"],
        "topic": inp.get("topic", ""),
        "reason": inp.get("reason", ""),
        "at": agent_mod.now_iso(),
    }
    with agent_mod.FOLLOWUP_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _save_session(conv: dict) -> None:
    fin = conv.get("finish") or {}
    needs = fin.get("needs_followup") or [e.get("topic") for e in conv["escalations"]]
    agent_mod.save_session(
        conv["patient_id"],
        {
            "phase": conv["phase"],
            "at": agent_mod.now_iso(),
            "covered": fin.get(
                "covered", [r["topic"] for r in conv["recorded"] if r.get("correct")]
            ),
            "needs_followup": needs,
        },
    )


def _run_agent(conv: dict) -> list[dict]:
    """Drive the agent until it asks the next question or finishes.

    Returns the display events produced this stretch. On `ask` we suspend (save
    the pending tool_use id + any held results) so the patient can answer next.
    """
    events: list[dict] = []
    steps = 0
    while not conv["finished"] and steps < MAX_AGENT_STEPS:
        steps += 1
        resp = _client.messages.create(
            model=agent_mod.MODEL,
            max_tokens=1500,
            system=SYSTEM,
            tools=agent_mod.TOOLS,
            messages=conv["messages"],
        )
        conv["messages"].append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            events.append({"type": "finish", "text": text or FINISH_TEXT})
            conv["finished"] = True
            break

        tool_results: list[dict] = []
        ask_block = None
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "ask":
                ask_block = block
                events.append({"type": "question", "text": block.input["question_text"]})
            elif block.name == "record":
                conv["recorded"].append(block.input)
                events.append({"type": "note", "text": block.input.get("note", "")})
                tool_results.append(_tool_result(block.id, "recorded"))
            elif block.name == "escalate":
                _write_escalation(conv, block.input)
                conv["escalations"].append(block.input)
                events.append({"type": "escalate", "text": block.input.get("topic", "")})
                tool_results.append(_tool_result(block.id, "escalated to the care team"))
            elif block.name == "finish":
                conv["finish"] = block.input
                conv["finished"] = True
                events.append({"type": "finish", "text": FINISH_TEXT})
                tool_results.append(_tool_result(block.id, "session ended"))

        if ask_block is not None:
            # Suspend for the patient's answer; hold record/escalate results so
            # the whole tool-result batch goes back together next turn.
            conv["pending"] = {"ask_id": ask_block.id, "held_results": tool_results}
            break

        # No ask this turn — return the tool results so the transcript stays valid.
        if tool_results:
            conv["messages"].append({"role": "user", "content": tool_results})

    if conv["finished"] and not conv.get("saved"):
        _save_session(conv)
        conv["saved"] = True
    return events


def _start_conversation(question_id: int, answer: str) -> tuple[str, list[dict], bool]:
    question = question_by_id(question_id)
    if question is None:
        raise KeyError(question_id)

    questions = json.loads(QUESTIONS_FILE.read_text())["questions"]
    available = [q for q in questions if q["care_phase"] in (PHASE, "both")]
    prior = agent_mod.load_prior_misses(PATIENT_ID)
    prior_line = (
        "In a PRIOR session this patient MISSED these topics — re-verify them "
        f"FIRST: {json.dumps(prior)}"
        if prior
        else "No prior sessions on record for this patient."
    )

    kickoff = (
        f"Patient: {PATIENT_ID}. Current phase: {PHASE}.\n\n"
        f"{prior_line}\n\n"
        "Here are the prepared questions available to you (with answer keys and "
        "the verbatim class line for each):\n\n"
        f"{json.dumps(available, indent=2)}\n\n"
        "You ALREADY asked this question and received the patient's answer. Judge "
        "it (call record), escalate if it is a missed red flag, then continue "
        "toward your goal — ask a follow-up or the next question, or finish:\n"
        f"Question: {question['question']} (id: {question['id']})\n"
        f'Patient\'s answer: "{answer}"\n\n'
        "Begin."
    )

    cid = uuid.uuid4().hex[:8]
    conv = {
        "messages": [{"role": "user", "content": kickoff}],
        "patient_id": PATIENT_ID,
        "phase": PHASE,
        "pending": None,
        "finished": False,
        "recorded": [],
        "escalations": [],
        "finish": None,
    }
    _conversations[cid] = conv
    events = _run_agent(conv)
    return cid, events, conv["finished"]


def _continue_conversation(cid: str, answer: str) -> tuple[list[dict], bool]:
    conv = _conversations.get(cid)
    if conv is None:
        raise KeyError(cid)
    pending = conv.get("pending")
    if pending is None:
        return [], conv["finished"]  # nothing awaiting an answer
    ask_result = _tool_result(pending["ask_id"], f'The patient answered: "{answer}"')
    conv["messages"].append(
        {"role": "user", "content": pending["held_results"] + [ask_result]}
    )
    conv["pending"] = None
    events = _run_agent(conv)
    return events, conv["finished"]


@app.post("/api/answer")
def api_answer(payload: AnswerIn) -> JSONResponse:
    try:
        if payload.conversation_id is not None:
            events, done = _continue_conversation(payload.conversation_id, payload.answer)
            cid = payload.conversation_id
        elif payload.question_id is not None:
            cid, events, done = _start_conversation(payload.question_id, payload.answer)
        else:
            return JSONResponse(
                {"error": "bad_request", "detail": "question_id or conversation_id required"},
                status_code=400,
            )
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except Exception as exc:  # noqa: BLE001 - surface agent/model failures
        return JSONResponse(
            {"error": "agent_failed", "detail": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return JSONResponse({"conversation_id": cid, "events": events, "done": done})


# Mount the frontend last so /api/* routes take precedence.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
