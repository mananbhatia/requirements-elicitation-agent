"""
Turn evaluator node — node 1 of the evaluation pipeline.

Iterates through the interview transcript, identifies every consultant turn,
classifies it by type, then evaluates it accordingly:

  question             → evaluated against the 14 mistake types
  solution_proposal    → is_well_formed not applicable; no mistake evaluation
  explanation          → skipped (consultant responding to client clarification request)
  acknowledgment       → skipped (filler with no discovery value)
  unproductive_statement → flagged; is_well_formed=False

All turns are included in turn_annotations regardless of type.
Downstream nodes use the turn_type field to route appropriately.

Annotation dict shape:
  {
    "turn_index": int,
    "turn_type": "question" | "solution_proposal" | "explanation" | "acknowledgment" | "unproductive_statement",
    "question": str,           # the consultant's message text
    "mistakes": list,          # populated for questions and unproductive_statements
    "is_well_formed": bool | None  # None for non-question types
  }
"""

from langchain_core.messages import HumanMessage as LCHumanMessage

from evaluation_state import EvaluationState
from evaluator_core import format_transcript, format_transcript_up_to, evaluate_turn_routed


def turn_evaluator(state: EvaluationState) -> dict:
    messages = state["transcript"]
    maturity = state.get("maturity", "")
    briefing = state.get("briefing", "")
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

        truncated_text = format_transcript_up_to(messages, turn_index)
        annotation = evaluate_turn_routed(
            content, transcript_text, turn_index,
            maturity_level=maturity,
            briefing=briefing,
            truncated_transcript_text=truncated_text,
        )
        if annotation is None:
            print(f"[EVAL]   Classification or evaluation failed, skipping.")
            continue

        annotation["question"] = content

        turn_type = annotation.get("turn_type", "question")
        wf = annotation.get("is_well_formed")
        mistakes = annotation.get("mistakes", [])

        print(f"[EVAL]   Type: {turn_type}")
        if mistakes:
            print(f"[EVAL]   Mistakes: {[m['mistake_type'] for m in mistakes]}")
        else:
            print(f"[EVAL]   Mistakes: none")
        print(f"[EVAL]   Well-formed: {wf}")

        annotations.append(annotation)

    return {"turn_annotations": annotations}
