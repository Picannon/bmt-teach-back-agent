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
                        "description": "True if safety-critical (fever, rash, missed dose, etc.)",
                    },
                },
                "required": [
                    "topic",
                    "question",
                    "source_quote",
                    "expected_answer",
                    "red_flag",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


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
    return json.loads(text)


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
    args = parser.parse_args()

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