"""
Turn evaluator node.

Iterates through the interview transcript, identifies every consultant turn,
and makes one LLM call per turn to classify mistakes against Shen et al.'s
14 mistake types.

Returns a list of turn_annotations — one dict per consultant turn:
  {
    "turn_index": int,
    "question": str,
    "mistakes": [{"mistake_type": str, "explanation": str}],
    "is_well_formed": bool,
    "information_elicited": bool
  }
"""

from langchain_core.messages import HumanMessage as LCHumanMessage

from evaluation_state import EvaluationState
from evaluator_core import format_transcript, evaluate_turn


def turn_evaluator(state: EvaluationState) -> dict:
    messages = state["transcript"]
    transcript_text = format_transcript(messages)
    annotations = []
    turn_index = 0

    for message in messages:
        is_human = isinstance(message, LCHumanMessage) or (
            isinstance(message, dict) and message.get("type") == "human"
        )
        if not is_human:
            continue

        content = message.content if hasattr(message, "content") else message.get("content", "")

        # Skip the hidden opening prompt injected by main.py.
        if content.startswith("[Start of interview"):
            continue

        turn_index += 1
        print(f"[EVAL] Turn {turn_index}: {content!r}")

        annotation = evaluate_turn(content, transcript_text, turn_index)
        if annotation is None:
            print(f"[EVAL]   Failed to evaluate turn {turn_index}, skipping.")
            continue

        annotation["question"] = content

        mistakes = annotation.get("mistakes", [])
        well_formed = annotation.get("is_well_formed", True)
        info_elicited = annotation.get("information_elicited", True)
        if mistakes:
            print(f"[EVAL]   Mistakes: {[m['mistake_type'] for m in mistakes]}")
        else:
            print(f"[EVAL]   No mistakes found.")
        print(f"[EVAL]   Well-formed: {well_formed} | Information elicited: {info_elicited}")

        annotations.append(annotation)

    return {"turn_annotations": annotations}
