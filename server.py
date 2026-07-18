"""FastAPI server for the BMT teach-back app.

Serves the mobile web app + the Part 2 endpoint surface (all one origin, no CORS):

  GET  /                       -> home page (static/index.html)
  GET  /question.html          -> question page (static/question.html)
  GET  /api/questions/{id}     -> one question (patient-facing; no answer key)
  POST /api/notify             -> laptop enqueues a notification
  GET  /api/notifications      -> phone polls for new notifications
  POST /api/answer             -> patient submits an answer; agent evaluates

Run locally:  ./.venv/bin/uvicorn server:app --reload
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

QUESTIONS_FILE = Path("questions.json")

app = FastAPI(title="BMT Teach-Back")

# In-memory notification queue. Single uvicorn process -> fine for the demo.
_notifications: list[dict] = []
_next_id = 1


def question_by_id(qid: int) -> dict | None:
    """Look up one question in questions.json by its baked-in id."""
    questions = json.loads(QUESTIONS_FILE.read_text())["questions"]
    return next((q for q in questions if q.get("id") == qid), None)


# --- request models ---------------------------------------------------------

class NotifyIn(BaseModel):
    question_id: int
    title: str | None = None
    body: str | None = None


class AnswerIn(BaseModel):
    question_id: int
    answer: str


# --- evaluation hook --------------------------------------------------------

def evaluate(question: dict, answer: str) -> dict:
    """Call the friend's evaluation.py if present; placeholder until it is.

    Contract: evaluation.evaluate_answer(question: dict, answer: str) -> dict.
    We only fall back on ImportError/AttributeError (module or function not yet
    there). Any error raised *inside* a real evaluate_answer propagates so the
    /api/answer handler can surface it.
    """
    try:
        import evaluation  # your friend's module (not created/edited here)

        return evaluation.evaluate_answer(question, answer)
    except (ImportError, AttributeError):
        return {"verdict": "placeholder", "feedback": "success!"}


# --- endpoints --------------------------------------------------------------

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
    # Consume-on-read: hand back any pending notifications and clear the queue,
    # so the phone shows each one once without tracking ids client-side.
    global _notifications
    pending = _notifications
    _notifications = []
    return JSONResponse({"notifications": pending})


@app.post("/api/answer")
def api_answer(payload: AnswerIn) -> JSONResponse:
    question = question_by_id(payload.question_id)
    if question is None:
        return JSONResponse({"error": "question_not_found"}, status_code=404)
    try:
        evaluation_result = evaluate(question, payload.answer)
    except Exception as exc:  # noqa: BLE001 - a real evaluate_answer failed
        return JSONResponse(
            {"error": "evaluation_failed", "detail": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    # {question_id, answer, evaluation} — the shape that extends to a `turns`
    # list for multi-turn later (see docs/BMT_part_2_endpoint_surface.md).
    return JSONResponse(
        {
            "question_id": payload.question_id,
            "answer": payload.answer,
            "evaluation": evaluation_result,
        }
    )


# Mount the frontend last so /api/* routes take precedence. html=True serves
# static/index.html at "/" and static/question.html at "/question.html".
app.mount("/", StaticFiles(directory="static", html=True), name="static")