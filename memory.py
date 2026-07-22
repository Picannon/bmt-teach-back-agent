#!/usr/bin/env python3
"""Shared persistence for the BMT app — one SQLite file, no server.

This is the single store both sides of the system read and write:

- the VOICE side (`voice_agent.py`) saves the live class transcript and the
  question set it generates from it;
- the TEACH-BACK side (`server.py` / `teach_back_agent.py`) reads prior misses,
  saves each session, records escalations, and persists the in-progress agent
  conversation so a server restart mid-demo loses nothing.

It replaces the earlier scattered state: `sessions/*.json`,
`care_team_followup.jsonl`, the on-disk `questions.json`, and the in-memory
`_conversations` dict in `server.py`.

Deliberately plain: stdlib `sqlite3`, JSON blobs for the loose/nested bits,
explicit columns for anything we query on. No ORM, no vector store — the domain
is a handful of clinical topics and we want the records auditable.

Supabase upgrade path: keep every DB call in this file. To move to hosted
Postgres + realtime later, swap the bodies here for the Supabase client; no other
module imports `sqlite3`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("bmt.db")

# One connection, guarded by a lock. FastAPI runs these handlers in a threadpool,
# so `check_same_thread=False` + a lock keeps writes serialized and safe.
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    armed_at   TEXT,               -- ISO time the agent may start listening; NULL = never
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    label      TEXT,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'live',   -- 'live' | 'synthetic'
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS question_sets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id INTEGER,
    patient_id    TEXT NOT NULL,
    questions_json TEXT NOT NULL,               -- the full [{id, question, expected_answer, ...}] list
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teachback_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id       TEXT NOT NULL,
    phase            TEXT NOT NULL,
    covered_json     TEXT NOT NULL DEFAULT '[]',
    needs_followup_json TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    phase      TEXT,
    topic      TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,              -- the short conversation_id used by /api/answer
    patient_id   TEXT NOT NULL,
    phase        TEXT NOT NULL,
    messages_json TEXT NOT NULL,                -- raw agent transcript (server-only; has answer keys)
    pending_json  TEXT,                         -- the unanswered {ask_id, held_results} | NULL
    finished     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open (once) and initialize the database. Idempotent."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def _db() -> sqlite3.Connection:
    return connect()


# ── patients / arm gate ─────────────────────────────────────────────────────

def upsert_patient(patient_id: str, name: str | None = None,
                   armed_at: str | None = None) -> None:
    with _lock:
        db = _db()
        row = db.execute("SELECT id FROM patients WHERE id=?", (patient_id,)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO patients (id, name, armed_at, created_at) VALUES (?,?,?,?)",
                (patient_id, name, armed_at, now_iso()),
            )
        else:
            # Only overwrite fields that were actually supplied.
            if name is not None:
                db.execute("UPDATE patients SET name=? WHERE id=?", (name, patient_id))
            if armed_at is not None:
                db.execute("UPDATE patients SET armed_at=? WHERE id=?", (armed_at, patient_id))
        db.commit()


def get_patient(patient_id: str) -> dict | None:
    row = _db().execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    return dict(row) if row else None


def is_armed(patient_id: str, at: datetime | None = None) -> bool:
    """True once the current time has reached the patient's armed_at."""
    p = get_patient(patient_id)
    if not p or not p.get("armed_at"):
        return False
    now = at or datetime.now(timezone.utc)
    try:
        armed = datetime.fromisoformat(p["armed_at"])
    except ValueError:
        return False
    if armed.tzinfo is None:
        armed = armed.replace(tzinfo=timezone.utc)
    return now >= armed


def arm_now(patient_id: str, name: str | None = None) -> str:
    """Demo override — arm the patient effective immediately. Returns armed_at."""
    ts = now_iso()
    upsert_patient(patient_id, name=name, armed_at=ts)
    return ts


# ── transcripts + generated question sets (voice handoff) ───────────────────

def save_transcript(patient_id: str, text: str, label: str | None = None,
                    source: str = "live") -> int:
    with _lock:
        db = _db()
        cur = db.execute(
            "INSERT INTO transcripts (patient_id, label, text, source, created_at) "
            "VALUES (?,?,?,?,?)",
            (patient_id, label, text, source, now_iso()),
        )
        db.commit()
        return int(cur.lastrowid)


def save_questions(patient_id: str, questions: list[dict],
                   transcript_id: int | None = None) -> int:
    with _lock:
        db = _db()
        cur = db.execute(
            "INSERT INTO question_sets (transcript_id, patient_id, questions_json, created_at) "
            "VALUES (?,?,?,?)",
            (transcript_id, patient_id, json.dumps(questions), now_iso()),
        )
        db.commit()
        return int(cur.lastrowid)


def latest_questions(patient_id: str | None = None) -> list[dict]:
    """Most recently generated question set (per patient if given, else global)."""
    if patient_id is None:
        row = _db().execute(
            "SELECT questions_json FROM question_sets ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = _db().execute(
            "SELECT questions_json FROM question_sets WHERE patient_id=? ORDER BY id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    return json.loads(row["questions_json"]) if row else []


def question_by_id(qid, patient_id: str | None = None) -> dict | None:
    for q in latest_questions(patient_id):
        if str(q.get("id")) == str(qid):
            return q
    return None


# ── teach-back sessions = the cross-journey agent memory ────────────────────

def load_prior_misses(patient_id: str) -> list[dict]:
    """What this patient MISSED in earlier sessions — the memory that spans the
    journey. Same shape teach_back_agent expected from the old JSON version."""
    rows = _db().execute(
        "SELECT phase, needs_followup_json FROM teachback_sessions "
        "WHERE patient_id=? ORDER BY id",
        (patient_id,),
    ).fetchall()
    prior = []
    for r in rows:
        for topic in json.loads(r["needs_followup_json"]):
            prior.append({"topic": topic, "phase": r["phase"]})
    return prior


def save_session(patient_id: str, record: dict) -> None:
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO teachback_sessions "
            "(patient_id, phase, covered_json, needs_followup_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                patient_id,
                record.get("phase", ""),
                json.dumps(record.get("covered", [])),
                json.dumps(record.get("needs_followup", [])),
                record.get("at", now_iso()),
            ),
        )
        db.commit()


def write_escalation(entry: dict) -> None:
    """Record a real care-team follow-up. Replaces the care_team_followup.jsonl append."""
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO escalations (patient_id, phase, topic, reason, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                entry.get("patient_id", ""),
                entry.get("phase", ""),
                entry.get("topic", ""),
                entry.get("reason", ""),
                entry.get("at", now_iso()),
            ),
        )
        db.commit()


def list_escalations(patient_id: str | None = None) -> list[dict]:
    if patient_id is None:
        rows = _db().execute("SELECT * FROM escalations ORDER BY id DESC").fetchall()
    else:
        rows = _db().execute(
            "SELECT * FROM escalations WHERE patient_id=? ORDER BY id DESC", (patient_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── in-progress teach-back conversations (survive a restart) ────────────────

def save_conversation(cid: str, conv: dict) -> None:
    """Persist the resumable agent conversation. `conv` is the dict server.py keeps;
    we store only what's needed to resume (raw messages + pending + flags)."""
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO conversations (id, patient_id, phase, messages_json, pending_json, "
            "finished, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET messages_json=excluded.messages_json, "
            "pending_json=excluded.pending_json, finished=excluded.finished",
            (
                cid,
                conv.get("patient_id", ""),
                conv.get("phase", ""),
                json.dumps(conv.get("messages", []), default=_json_default),
                json.dumps(conv.get("pending")) if conv.get("pending") else None,
                1 if conv.get("finished") else 0,
                now_iso(),
            ),
        )
        db.commit()


def load_conversation(cid: str) -> dict | None:
    row = _db().execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    return {
        "patient_id": row["patient_id"],
        "phase": row["phase"],
        "messages": json.loads(row["messages_json"]),
        "pending": json.loads(row["pending_json"]) if row["pending_json"] else None,
        "finished": bool(row["finished"]),
    }


def _json_default(o):
    # Anthropic SDK content blocks aren't plain dicts; fall back to their dict form.
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "to_dict"):
        return o.to_dict()
    return str(o)


if __name__ == "__main__":  # tiny smoke test
    connect()
    upsert_patient("demo-patient", name="Demo", armed_at=None)
    print("armed?", is_armed("demo-patient"))
    print("armed at", arm_now("demo-patient"))
    print("armed?", is_armed("demo-patient"))
    tid = save_transcript("demo-patient", "hello class transplant", label="test")
    save_questions("demo-patient", [{"id": "q1", "question": "?"}], transcript_id=tid)
    print("latest questions:", latest_questions("demo-patient"))
    print("OK — bmt.db initialized")
