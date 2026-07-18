"""FastAPI server for the BMT teach-back app.

Serves two things from one origin (keeps it simple, no CORS):
  - GET /api/questions  -> teach-back questions as JSON (wraps generate_questions)
  - GET /               -> the mobile-styled web app (static/index.html)

Run locally:  ./.venv/bin/uvicorn server:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from generate_questions import generate_questions

app = FastAPI(title="BMT Teach-Back")


@app.get("/api/questions")
def api_questions() -> JSONResponse:
    """Generate teach-back questions from the visit transcript.

    Calls Claude live (needs ANTHROPIC_API_KEY). Returns a JSON error rather
    than a stack trace if the key is missing or the call fails, so the frontend
    can show something sensible.
    """
    try:
        transcript = Path("voice-transcript.txt").read_text()
        prompt = Path("prompt.md").read_text()
        return JSONResponse(generate_questions(transcript, prompt))
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        return JSONResponse(
            {"error": type(exc).__name__, "detail": str(exc)},
            status_code=500,
        )


# Mount the frontend last so /api/* routes take precedence. html=True serves
# static/index.html at "/".
app.mount("/", StaticFiles(directory="static", html=True), name="static")