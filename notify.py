#!/usr/bin/env python3
"""Send a teach-back notification to the patient's phone (run on the laptop).

Reads questions.json, picks a question, and POSTs it to the running server's
/api/notify endpoint. The phone (polling) then pops the notification.

Examples:
  python notify.py --id 3
  python notify.py --topic cellcept
  python notify.py --random          # default if no selector is given
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path

QUESTIONS_FILE = Path("questions.json")
DEFAULT_SERVER = "http://localhost:8000"


def pick_question_id(questions: list[dict], args: argparse.Namespace) -> int:
    """Choose which question to send, by --id / --topic / --random (default)."""
    if args.id is not None:
        if not any(q.get("id") == args.id for q in questions):
            sys.exit(f"--id {args.id} not found in {QUESTIONS_FILE}")
        return args.id
    if args.topic:
        for q in questions:
            if args.topic.lower() in q.get("topic", "").lower():
                return q["id"]
        sys.exit(f"No question with a topic matching '{args.topic}'")
    # default / --random
    return random.choice(questions)["id"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a teach-back notification to the phone."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--id", type=int, help="Question id to send")
    group.add_argument(
        "--topic", help="Send the first question whose topic contains this text"
    )
    group.add_argument(
        "--random", action="store_true", help="Send a random question (default)"
    )
    parser.add_argument("--title", help="Notification title (optional)")
    parser.add_argument(
        "--body", help="Notification body (optional; defaults to the question)"
    )
    parser.add_argument(
        "--server", default=DEFAULT_SERVER, help=f"Server base URL (default: {DEFAULT_SERVER})"
    )
    args = parser.parse_args()

    if not QUESTIONS_FILE.exists():
        sys.exit(
            f"{QUESTIONS_FILE} not found. Generate it first:\n"
            "  python generate_questions.py --out questions.json"
        )
    questions = json.loads(QUESTIONS_FILE.read_text())["questions"]
    if not questions:
        sys.exit(f"{QUESTIONS_FILE} has no questions.")

    qid = pick_question_id(questions, args)

    payload: dict = {"question_id": qid}
    if args.title:
        payload["title"] = args.title
    if args.body:
        payload["body"] = args.body

    request = urllib.request.Request(
        f"{args.server}/api/notify",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach the server at {args.server}: {exc}")

    question_text = next(q["question"] for q in questions if q["id"] == qid)
    print(f"Sent notification #{result['id']} → question {qid}: {question_text}")


if __name__ == "__main__":
    main()