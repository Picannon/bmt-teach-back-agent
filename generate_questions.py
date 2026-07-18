#!/usr/bin/env python3
"""Generate teach-back questions from a BMT visit transcript.

Core:  generate_questions(transcript, prompt) -> dict   (validated JSON)
CLI:   python generate_questions.py [--transcript FILE] [--prompt FILE] [--out FILE]

The core function is what a future backend (e.g. the mobile app's server) will
import and call. The CLI below is just a thin wrapper for local runs — keep the
real logic in generate_questions() so the frontend can change without touching it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()  # pick up ANTHROPIC_API_KEY from a local .env if present

# Question generation is a one-shot, quality-sensitive batch step (NOT the
# real-time teach-back loop), so we use a capable model here. Swap to
# "claude-sonnet-5" to cut cost, or "claude-haiku-4-5" for speed.
MODEL = "claude-opus-4-8"

DEFAULT_TRANSCRIPT = "voice-transcript.txt"
DEFAULT_PROMPT = "prompt.md"
DEFAULT_QUESTIONS = "questions.json"

# JSON Schema the model is constrained to. Every question is grounded in a
# verbatim quote from the transcript so the UI can show where it came from.
QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Short subject label, e.g. 'tacrolimus timing'",
                    },
                    "question": {
                        "type": "string",
                        "description": "The teach-back question, in plain language",
                    },
                    "source_quote": {
                        "type": "string",
                        "description": "Verbatim transcript line this question checks",
                    },
                    "expected_answer": {
                        "type": "string",
                        "description": "What a correct patient answer must contain",
                    },
                    "red_flag": {
                        "type": "boolean",
                        "description": "True only for acute 'call the team now' items (fever, rash, rigors, etc.)",
                    },
                    "care_phase": {
                        "type": "string",
                        "enum": ["inpatient_admission", "discharge", "both"],
                        "description": "When to ask: admission knowledge check, discharge teach-back, or both",
                    },
                },
                "required": [
                    "topic",
                    "question",
                    "source_quote",
                    "expected_answer",
                    "red_flag",
                    "care_phase",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def _slugify(text: str) -> str:
    """Turn a topic label into a url-safe id fragment, e.g. 'fever-threshold'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "question"


def _assign_ids(questions: list[dict]) -> None:
    """Give each question a stable, unique `id` derived from its topic.

    The frontend sends this id back at submit time so the backend can look the
    question up server-side — the answer key never has to leave the server.
    """
    seen: dict[str, int] = {}
    for q in questions:
        base = _slugify(q["topic"])
        seen[base] = seen.get(base, 0) + 1
        q["id"] = base if seen[base] == 1 else f"{base}-{seen[base]}"


def generate_questions(transcript: str, prompt: str, model: str = MODEL) -> dict:
    """Return a validated teach-back question set for a visit transcript.

    This is the reusable core — the CLI and any future backend both call it.
    """
    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / ant profile

    response = client.messages.create(
        model=model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=prompt,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": QUESTION_SCHEMA},
        },
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is the visit transcript. Generate the teach-back "
                    "questions grounded in it.\n\n<transcript>\n"
                    f"{transcript}\n</transcript>"
                ),
            }
        ],
    )

    # With structured outputs the first text block is guaranteed valid JSON.
    text = next(block.text for block in response.content if block.type == "text")
    result = json.loads(text)
    _assign_ids(result.get("questions", []))
    return result


def filter_by_phase(questions: list[dict], phase: str) -> list[dict]:
    """Return the questions to ask at a given care phase.

    A phase-specific view includes questions tagged for that exact phase PLUS
    any tagged ``both`` (shared items like the tacrolimus/CellCept schedule that
    belong in both the admission check and the discharge teach-back).
    """
    return [q for q in questions if q["care_phase"] in (phase, "both")]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate BMT teach-back questions from a visit transcript."
    )
    parser.add_argument(
        "--transcript",
        default=DEFAULT_TRANSCRIPT,
        help=f"Transcript file (default: {DEFAULT_TRANSCRIPT})",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help=f"System prompt file (default: {DEFAULT_PROMPT})",
    )
    parser.add_argument(
        "--model", default=MODEL, help=f"Claude model ID (default: {MODEL})"
    )
    parser.add_argument("--out", help="Write JSON here instead of stdout")
    parser.add_argument(
        "--phase",
        choices=["inpatient_admission", "discharge"],
        help=(
            "Filter an already-generated file to one care phase instead of "
            "generating. Reads --from (no API call); includes 'both' questions."
        ),
    )
    parser.add_argument(
        "--from",
        dest="source",
        default=DEFAULT_QUESTIONS,
        help=f"File to filter when --phase is used (default: {DEFAULT_QUESTIONS})",
    )
    args = parser.parse_args()

    # --phase is a fast, offline view: read the saved questions and filter them.
    # No model call, so it's safe to run live during a demo.
    if args.phase:
        source = Path(args.source)
        if not source.exists():
            parser.error(
                f"{source} not found. Generate it first:\n"
                f"  python generate_questions.py --out {source}"
            )
        questions = json.loads(source.read_text())["questions"]
        filtered = filter_by_phase(questions, args.phase)
        output = json.dumps({"questions": filtered}, indent=2)
        if args.out:
            Path(args.out).write_text(output)
            print(
                f"Wrote {len(filtered)} '{args.phase}' questions to {args.out}",
                file=sys.stderr,
            )
        else:
            print(output)
        return

    transcript = Path(args.transcript).read_text()
    prompt = Path(args.prompt).read_text()

    result = generate_questions(transcript, prompt, model=args.model)
    output = json.dumps(result, indent=2)

    if args.out:
        Path(args.out).write_text(output)
        print(
            f"Wrote {len(result['questions'])} questions to {args.out}",
            file=sys.stderr,
        )
    else:
        print(output)


if __name__ == "__main__":
    main()