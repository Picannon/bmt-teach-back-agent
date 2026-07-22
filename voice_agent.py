#!/usr/bin/env python3
"""Live voice agent — ambient class capture, but only when it should be.

This is the container that REPLACES the static `voice-transcript.txt`. It is a
self-contained FastAPI router mounted into the main app (`server.py`). What makes
it an agent rather than a dumb recorder:

1. ARM GATE — it does nothing until the patient's `armed_at` time has passed
   ("it begins to listen on a certain day"). Before that it reports `asleep` and
   never opens a Deepgram socket, so no audio is transcribed at all.
2. KEYWORD WAKE — once armed it transcribes cheaply but only *captures* the
   conversation after it hears a trigger keyword (class / transplant / BMT /
   tacrolimus / CellCept…). After a silence window it drops back to listening.
3. HANDOFF — on stop it saves the captured transcript and calls the existing
   `generate_questions()` on the words that were actually spoken, then persists
   the question set. That is the moment the phone teach-back becomes available.

Audio path: the phone captures mic audio (MediaRecorder, webm/opus) and streams
binary chunks over the WebSocket at `/api/voice/stream`. The server proxies those
chunks to Deepgram's streaming API (the API key stays server-side) and receives
JSON transcripts back, which drive the state machine above.

State machine:  asleep → listening → capturing → listening → … → done
Every transition is pushed back over the socket so the UI can show a status pill.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import memory
from generate_questions import generate_questions

router = APIRouter(prefix="/api/voice", tags=["voice"])

PATIENT_ID = "demo-patient"          # single demo patient, matches server.py
PROMPT_FILE = Path("prompt.md")

# Words that WAKE capture. Kept lowercase; matched case-insensitively on finals.
TRIGGER_KEYWORDS = [
    "class", "transplant", "bmt", "graft", "gvhd", "rejection",
    "tacrolimus", "prograf", "fk506", "cellcept", "mycophenolate",
    "bactrim", "medication", "medicine", "immune", "infection",
]

# Deepgram term boosting so the medical vocabulary is transcribed accurately.
BOOST_TERMS = [
    "tacrolimus", "Prograf", "CellCept", "mycophenolate", "Bactrim",
    "GVHD", "graft-versus-host", "BMT", "transplant",
]

SILENCE_SECONDS = 8.0                 # capturing → listening after this quiet gap
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"


def _deepgram_url() -> str:
    # Containerized webm/opus from the browser: let Deepgram auto-detect the
    # container (no encoding/sample_rate params). nova-2-medical for clinical terms.
    params = {
        "model": "nova-2-medical",
        "language": "en-US",
        "smart_format": "true",
        "punctuate": "true",
        "interim_results": "true",
        "endpointing": "300",
    }
    query = urlencode(params) + "".join(f"&keywords={t}:2" for t in BOOST_TERMS)
    return f"{DEEPGRAM_URL}?{query}"


def _has_trigger(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in TRIGGER_KEYWORDS)


# ── REST: arm gate + status ─────────────────────────────────────────────────

class ArmIn(BaseModel):
    patient_id: Optional[str] = None
    name: Optional[str] = None


@router.post("/arm")
def arm(payload: ArmIn) -> JSONResponse:
    """Demo override for 'the day' — arm the patient effective now."""
    pid = payload.patient_id or PATIENT_ID
    armed_at = memory.arm_now(pid, name=payload.name)
    return JSONResponse({"patient_id": pid, "armed_at": armed_at, "armed": True})


SYNTHETIC_TRANSCRIPT = Path("voice-transcript.txt")


@router.post("/synthetic")
def synthetic(payload: ArmIn) -> JSONResponse:
    """Demo fallback — skip the mic, load the checked-in synthetic class transcript
    and run the exact same handoff (save transcript → generate questions). Lets the
    live demo proceed even if venue Wi-Fi or the mic fails."""
    pid = payload.patient_id or PATIENT_ID
    text = SYNTHETIC_TRANSCRIPT.read_text()
    tid = memory.save_transcript(pid, text, label="synthetic class", source="synthetic")
    result = generate_questions(text, PROMPT_FILE.read_text())
    questions = result.get("questions", [])
    memory.save_questions(pid, questions, transcript_id=tid)
    return JSONResponse({
        "patient_id": pid, "transcript_id": tid, "source": "synthetic",
        "question_ids": [q.get("id") for q in questions], "count": len(questions),
    })


@router.get("/status")
def status(patient_id: str | None = None) -> JSONResponse:
    pid = patient_id or PATIENT_ID
    p = memory.get_patient(pid)
    return JSONResponse({
        "patient_id": pid,
        "armed": memory.is_armed(pid),
        "armed_at": p.get("armed_at") if p else None,
        "deepgram_configured": bool(os.environ.get("DEEPGRAM_API_KEY")),
    })


# ── the streaming session ───────────────────────────────────────────────────

class _Session:
    """Per-connection state machine. One instance per WebSocket."""

    def __init__(self, ws: WebSocket, patient_id: str):
        self.ws = ws
        self.patient_id = patient_id
        self.state = "asleep"
        self.captured: list[str] = []          # final transcripts kept while capturing
        self.last_activity = asyncio.get_event_loop().time()
        self.stop = asyncio.Event()

    async def notify(self, **event) -> None:
        event.setdefault("state", self.state)
        try:
            await self.ws.send_text(json.dumps(event))
        except Exception:
            pass

    async def set_state(self, new: str) -> None:
        if new != self.state:
            self.state = new
            await self.notify(type="state")

    async def on_final(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.state == "listening" and _has_trigger(text):
            await self.set_state("capturing")
        if self.state == "capturing":
            self.captured.append(text)
            self.last_activity = asyncio.get_event_loop().time()
            await self.notify(type="capture", text=text,
                              transcript=" ".join(self.captured))

    async def watchdog(self) -> None:
        """Drop back to listening after a quiet gap so we don't capture forever."""
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            if self.state == "capturing":
                quiet = asyncio.get_event_loop().time() - self.last_activity
                if quiet > SILENCE_SECONDS:
                    await self.set_state("listening")

    async def finalize(self) -> None:
        """Handoff: persist the captured transcript and generate questions from it."""
        transcript = " ".join(self.captured).strip()
        if not transcript:
            await self.notify(type="done", captured=False,
                              message="Nothing on-topic was captured.")
            return
        await self.notify(type="generating", transcript=transcript)
        tid = memory.save_transcript(self.patient_id, transcript,
                                     label="live class", source="live")
        prompt = PROMPT_FILE.read_text()
        # generate_questions is a blocking Claude call — keep the event loop free.
        result = await asyncio.to_thread(generate_questions, transcript, prompt)
        questions = result.get("questions", [])
        memory.save_questions(self.patient_id, questions, transcript_id=tid)
        await self.set_state("done")
        await self.notify(type="done", captured=True, transcript_id=tid,
                          question_ids=[q.get("id") for q in questions],
                          count=len(questions))


async def _pump_deepgram(session: _Session, dg) -> None:
    """Read Deepgram results → drive the state machine + live UI."""
    async for raw in dg:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if msg.get("type") == "UtteranceEnd":
            continue
        alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
        transcript = alt.get("transcript", "")
        if not transcript:
            continue
        if msg.get("is_final"):
            await session.on_final(transcript)
        elif session.state == "capturing":
            # live interim text while actively capturing
            await session.notify(type="interim", text=transcript)


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()
    patient_id = ws.query_params.get("patient_id", PATIENT_ID)
    session = _Session(ws, patient_id)

    # Arm gate: if the day hasn't come, stay asleep and never open Deepgram.
    if not memory.is_armed(patient_id):
        await session.notify(type="state", message="Agent is asleep until its armed day.")
        try:
            while True:
                await ws.receive()          # drain/ignore until the client leaves
        except WebSocketDisconnect:
            return
        return

    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        await session.notify(type="error",
                             message="DEEPGRAM_API_KEY not set on the server.")
        await ws.close()
        return

    await session.set_state("listening")

    try:
        async with websockets.connect(
            _deepgram_url(),
            additional_headers={"Authorization": f"Token {api_key}"},
            max_size=None,
        ) as dg:
            dg_task = asyncio.create_task(_pump_deepgram(session, dg))
            dog_task = asyncio.create_task(session.watchdog())
            try:
                while True:
                    frame = await ws.receive()
                    if frame.get("type") == "websocket.disconnect":
                        break
                    if frame.get("bytes") is not None:
                        await dg.send(frame["bytes"])          # audio → Deepgram
                    elif frame.get("text") is not None:
                        ctrl = _parse_ctrl(frame["text"])
                        if ctrl == "stop":
                            break
            except WebSocketDisconnect:
                pass
            finally:
                session.stop.set()
                # tell Deepgram we're done so it flushes any final result
                try:
                    await dg.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    pass
                await asyncio.sleep(0.3)
                dg_task.cancel()
                dog_task.cancel()
            await session.finalize()
    except Exception as exc:  # noqa: BLE001 — surface connection/model errors to the UI
        await session.notify(type="error", message=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def _parse_ctrl(text: str) -> str | None:
    try:
        return json.loads(text).get("type")
    except (ValueError, TypeError):
        return text.strip() or None
