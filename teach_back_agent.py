#!/usr/bin/env python3
"""Agentic BMT teach-back — a tool-using agent, not a fixed quiz.

What makes this an agent (vs. teach_back.py's fixed loop):

1. AUTONOMY — the model runs a tool-use loop and decides the next action itself:
   ask a prepared question, ask an adaptive follow-up probe, record a judgment,
   escalate a safety gap, or finish. Nothing hard-codes the order.
2. MEMORY across the journey — it loads what this patient MISSED in an earlier
   session (e.g. the admission knowledge check) and is told to re-verify those
   first this session (e.g. the discharge teach-back). Sessions persist to disk.
3. REAL ACTIONS — `escalate` writes an actual care-team follow-up record to
   disk. A missed red flag becomes a durable action, not just printed text.

The goal handed to the agent: verify the patient has retained the safety-critical
(red-flag) knowledge, re-checking prior misses first. It pursues that goal and
decides when it's done.

CLI:  python teach_back_agent.py [--patient ID] [--phase PHASE] [--questions FILE]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-4-8"  # project decision: Opus everywhere

DEFAULT_QUESTIONS = "questions.json"
SESSIONS_DIR = Path("sessions")            # per-patient memory across sessions
FOLLOWUP_FILE = Path("care_team_followup.jsonl")  # real escalation actions land here


SYSTEM = """You are an autonomous BMT (blood & marrow transplant) teach-back agent.

Your GOAL: verify the patient has actually retained the SAFETY-CRITICAL
(red-flag) knowledge from their education class, and cover the key medication
knowledge. You are not reading a fixed quiz — YOU decide what to do next.

You have tools: ask, record, escalate, finish. Work in a loop:
- Prioritize red-flag topics, and re-verify anything the patient missed in a
  PRIOR session first (you'll be told what those were).
- Call `ask` to pose ONE question at a time. Use a prepared question_id when a
  good one exists; write an adaptive follow-up probe (still grounded in the class
  content you're given) when the patient was wrong and you want to dig in or
  re-teach.
- After each answer, judge it against the expected answer you were given, then
  call `record`. If the patient's answer is WRONG on a red-flag topic, you MUST
  also call `escalate`, and you should re-teach by quoting the class line
  verbatim in your next `ask` probe before moving on.
- When every red-flag topic has been verified (or you've made a reasonable
  attempt at the ones they keep missing), call `finish` with a summary.

Keep spoken text warm, plain, second-person, at about a 6th-grade level. When you
re-teach, quote the class line verbatim."""


# ── tools the agent can call ────────────────────────────────────────────────
TOOLS = [
    {
        "name": "ask",
        "description": (
            "Ask the patient ONE question out loud and get their answer. Give a "
            "prepared question's question_id when one fits, and always include the "
            "exact question_text to speak (write your own for an adaptive probe)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_text": {
                    "type": "string",
                    "description": "The exact question to speak to the patient",
                },
                "question_id": {
                    "type": "string",
                    "description": "id of the prepared question, or omit for a custom probe",
                },
            },
            "required": ["question_text"],
        },
    },
    {
        "name": "record",
        "description": "Record your judgment of the patient's most recent answer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "correct": {"type": "boolean"},
                "note": {"type": "string", "description": "one line: what they got right/wrong"},
            },
            "required": ["topic", "correct", "note"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Flag a safety-critical gap for the care team. Writes a REAL follow-up "
            "record to disk. Use whenever a red-flag topic is missed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["topic", "reason"],
        },
    },
    {
        "name": "finish",
        "description": "End the session with a summary once the goal is met.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "covered": {"type": "array", "items": {"type": "string"}},
                "needs_followup": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_prior_misses(patient_id: str) -> list[dict]:
    """Read this patient's earlier sessions — the memory that spans the journey."""
    path = SESSIONS_DIR / f"{patient_id}.json"
    if not path.exists():
        return []
    sessions = json.loads(path.read_text())
    prior = []
    for s in sessions:
        for topic in s.get("needs_followup", []):
            prior.append({"topic": topic, "phase": s.get("phase", "?")})
    return prior


def save_session(patient_id: str, record: dict) -> None:
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = SESSIONS_DIR / f"{patient_id}.json"
    sessions = json.loads(path.read_text()) if path.exists() else []
    sessions.append(record)
    path.write_text(json.dumps(sessions, indent=2))


class TeachBackAgent:
    def __init__(self, questions: list[dict], patient_id: str, phase: str,
                 model: str = MODEL):
        self.questions = questions
        self.patient_id = patient_id
        self.phase = phase
        self.model = model
        self.client = anthropic.Anthropic()
        self.recorded: list[dict] = []
        self.escalations: list[dict] = []
        self.finished: dict | None = None

    # ── tool handlers ───────────────────────────────────────────────────────
    def _do_ask(self, inp: dict) -> str:
        print(f"\n🗣  {inp['question_text']}")
        try:
            answer = input("   patient> ").strip()
        except EOFError:
            answer = ""
        if not answer:
            return "The patient gave no answer. Consider finishing."
        return f'The patient answered: "{answer}"'

    def _do_record(self, inp: dict) -> str:
        self.recorded.append(inp)
        mark = "✓" if inp["correct"] else "✗"
        print(f"   [{mark} recorded: {inp['topic']} — {inp['note']}]")
        return "recorded"

    def _do_escalate(self, inp: dict) -> str:
        entry = {
            "patient_id": self.patient_id,
            "phase": self.phase,
            "topic": inp["topic"],
            "reason": inp["reason"],
            "at": now_iso(),
        }
        self.escalations.append(entry)
        with FOLLOWUP_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"   [⚠ ESCALATED to care team: {inp['topic']} → {FOLLOWUP_FILE}]")
        return "escalated and written to the care-team follow-up list"

    def _do_finish(self, inp: dict) -> str:
        self.finished = inp
        return "session ended"

    def _handle(self, name: str, inp: dict) -> str:
        return {
            "ask": self._do_ask,
            "record": self._do_record,
            "escalate": self._do_escalate,
            "finish": self._do_finish,
        }[name](inp)

    # ── the agent loop ──────────────────────────────────────────────────────
    def run(self) -> None:
        prior = load_prior_misses(self.patient_id)
        available = [q for q in self.questions
                     if not self.phase or q["care_phase"] in (self.phase, "both")]

        prior_line = (
            "In a PRIOR session this patient MISSED these topics — re-verify them "
            f"FIRST: {json.dumps(prior)}"
            if prior else
            "No prior sessions on record for this patient."
        )

        kickoff = (
            f"Patient: {self.patient_id}. Current phase: {self.phase}.\n\n"
            f"{prior_line}\n\n"
            "Here are the prepared questions available to you (with the answer keys "
            "and the verbatim class line for each). Ask them one at a time, adapt "
            "when the patient is wrong, and pursue your goal:\n\n"
            f"{json.dumps(available, indent=2)}\n\n"
            "Begin."
        )

        messages = [{"role": "user", "content": kickoff}]
        print(f"\n=== Teach-back agent · patient {self.patient_id} · {self.phase} ===")
        if prior:
            print(f"(memory: re-checking {len(prior)} topic(s) missed earlier)")

        while self.finished is None:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            # surface any narration the agent speaks
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print(f"   {block.text.strip()}")

            if resp.stop_reason != "tool_use":
                break

            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = self._handle(block.name, block.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": out,
                    })
            messages.append({"role": "user", "content": results})

        self._wrap_up()

    def _wrap_up(self) -> None:
        f = self.finished or {}
        needs = f.get("needs_followup") or [e["topic"] for e in self.escalations]
        print("\n" + "=" * 54)
        print(f" {f.get('summary', 'Session ended.')}")
        if f.get("covered"):
            print(f" Verified: {', '.join(f['covered'])}")
        if needs:
            print(f" Needs follow-up: {', '.join(needs)}")
        if self.escalations:
            print(f" ⚠ {len(self.escalations)} escalation(s) written to {FOLLOWUP_FILE}")
        print("=" * 54)

        # persist this session so the NEXT phase can pick up the misses
        save_session(self.patient_id, {
            "phase": self.phase,
            "at": now_iso(),
            "covered": f.get("covered", [r["topic"] for r in self.recorded if r["correct"]]),
            "needs_followup": needs,
        })
        print(f"(session saved to {SESSIONS_DIR / (self.patient_id + '.json')})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic BMT teach-back.")
    parser.add_argument("--patient", default="demo-patient", help="Patient id (for memory)")
    parser.add_argument("--phase", choices=["inpatient_admission", "discharge"],
                        default="discharge", help="Care phase for this session")
    parser.add_argument("--questions", default=DEFAULT_QUESTIONS)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()

    path = Path(args.questions)
    if not path.exists():
        parser.error(f"{path} not found. Run generate_questions.py first.")
    questions = json.loads(path.read_text())["questions"]

    agent = TeachBackAgent(questions, args.patient, args.phase, model=args.model)
    agent.run()


if __name__ == "__main__":
    main()
