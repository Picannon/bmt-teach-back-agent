#!/usr/bin/env python3
"""Interactive teach-back loop — the ask–tell closed loop.

For each question we ASK it, LISTEN for the patient's typed answer, EVALUATE it
with a single Claude call, and on a wrong answer TELL: quote the class transcript
line verbatim, then explain it in plain language. One remediation per question,
then move on. A summary at the end lists what was missed, flagging any
safety-critical (red-flag) gaps for the care team.

This terminal loop is the stand-in for the voice loop — the turn logic here is
exactly what a voice frontend (or the FastAPI backend) would wrap later.

CLI:  python teach_back.py [--questions FILE] [--phase PHASE] [--model MODEL]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()  # pick up ANTHROPIC_API_KEY from a local .env if present

# Opus for every step, by project decision — we prioritize evaluation quality
# over per-turn latency here. If the live loop feels too slow in the demo, drop
# back to Haiku with `--model claude-haiku-4-5-20251001`.
MODEL = "claude-opus-4-8"

DEFAULT_QUESTIONS = "questions.json"

# One Claude call per turn returns both the verdict and what to say back.
EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {
            "type": "boolean",
            "description": "True if the patient's answer captures the essential expected content",
        },
        "feedback": {
            "type": "string",
            "description": "What to say back to the patient, read aloud, in the second person",
        },
    },
    "required": ["correct", "feedback"],
    "additionalProperties": False,
}

EVAL_SYSTEM = """You are a warm, plain-spoken BMT nurse running a teach-back check.

You are given one question, the expected answer, the verbatim line from the
patient's education class that the question checks, and the patient's answer.

Decide if the patient's answer captures the ESSENTIAL content of the expected
answer. Partial but safe counts as correct; missing a safety-critical detail does
not.

Return two fields:
- correct: true or false.
- feedback: what you SAY to the patient, out loud, in the second person.
    - If correct: confirm warmly in one sentence. Add nothing new.
    - If incorrect or incomplete: FIRST quote the class line verbatim, word for
      word, introduced naturally (for example: In your class we said, "..."). Then
      explain it in one or two short, plain sentences at about a 6th-grade level.
      Do not add anything that is not in that class line.

Write feedback to be read aloud: no markdown, no bullet points, no headings."""


def evaluate_turn(client: anthropic.Anthropic, question: dict, patient_answer: str,
                  model: str = MODEL) -> dict:
    """Evaluate one answer and return {"correct": bool, "feedback": str}."""
    user = (
        f"Question: {question['question']}\n"
        f"Expected answer: {question['expected_answer']}\n"
        f"Verbatim class line (quote this exactly if the patient is wrong): "
        f"\"{question['source_quote']}\"\n\n"
        f"Patient's answer: {patient_answer}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=600,
        system=EVAL_SYSTEM,
        output_config={
            "format": {"type": "json_schema", "schema": EVAL_SCHEMA},
        },
        messages=[{"role": "user", "content": user}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def evaluate_answer(question_id: str, patient_answer: str,
                    questions_path: str = DEFAULT_QUESTIONS,
                    client: anthropic.Anthropic | None = None) -> dict:
    """App-facing adapter: look up a question by id and evaluate one answer.

    This is the single call a backend (e.g. FastAPI) makes on submit. The
    frontend sends only {question_id, patient_answer}; the answer key
    (expected_answer, source_quote) is loaded here on the server and never sent
    to the browser. Returns {"correct": bool, "feedback": str} for display.
    """
    questions = json.loads(Path(questions_path).read_text())["questions"]
    question = next((q for q in questions if q.get("id") == question_id), None)
    if question is None:
        raise KeyError(f"No question with id {question_id!r} in {questions_path}")
    if client is None:
        client = anthropic.Anthropic()
    return evaluate_turn(client, question, patient_answer)


def run_loop(questions: list[dict], model: str = MODEL) -> list[dict]:
    """Ask every question in turn, evaluate + remediate, collect the results."""
    client = anthropic.Anthropic()
    results = []

    print(f"\nTeach-back — {len(questions)} questions.")
    print("Type your answer and press Enter. Type 'quit' to stop early.\n")

    for i, q in enumerate(questions, 1):
        flag = "  [RED FLAG]" if q.get("red_flag") else ""
        print(f"── Question {i}/{len(questions)} · {q['topic']}{flag} ──")
        print(f"  {q['question']}")
        answer = input("  Your answer: ").strip()

        if answer.lower() in {"quit", "exit"}:
            print("\nStopping early.")
            break

        verdict = evaluate_turn(client, q, answer, model=model)
        mark = "correct ✓" if verdict["correct"] else "needs review ✗"
        print(f"\n  {mark}")
        print(f"  {verdict['feedback']}\n")

        results.append({
            "topic": q["topic"],
            "red_flag": q.get("red_flag", False),
            "correct": verdict["correct"],
        })

    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        return
    missed = [r for r in results if not r["correct"]]
    red_missed = [r for r in missed if r["red_flag"]]

    print("=" * 52)
    print(f" Summary: {len(results) - len(missed)}/{len(results)} correct")
    if missed:
        print("\n Needs follow-up:")
        for r in missed:
            tag = " (RED FLAG)" if r["red_flag"] else ""
            print(f"   - {r['topic']}{tag}")
    if red_missed:
        print(f"\n ⚠ {len(red_missed)} safety-critical item(s) missed — flag for the care team.")
    print("=" * 52)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive BMT teach-back loop (ask–tell)."
    )
    parser.add_argument(
        "--questions",
        default=DEFAULT_QUESTIONS,
        help=f"Questions JSON to ask from (default: {DEFAULT_QUESTIONS})",
    )
    parser.add_argument(
        "--phase",
        choices=["inpatient_admission", "discharge"],
        help="Only ask questions for this care phase (includes 'both')",
    )
    parser.add_argument(
        "--model", default=MODEL, help=f"Claude model (default: {MODEL})"
    )
    args = parser.parse_args()

    path = Path(args.questions)
    if not path.exists():
        parser.error(
            f"{path} not found. Generate it first:\n"
            f"  python generate_questions.py --out {path}"
        )

    questions = json.loads(path.read_text())["questions"]
    if args.phase:
        questions = [q for q in questions if q["care_phase"] in (args.phase, "both")]

    results = run_loop(questions, model=args.model)
    print_summary(results)


if __name__ == "__main__":
    main()
