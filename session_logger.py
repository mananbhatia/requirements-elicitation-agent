"""
Session logger — saves complete session data to disk after each evaluation.

Output: logs/session_YYYY-MM-DD_HH-MM-SS.json
"""

import json
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, AIMessage


def _serialize_messages(messages: list) -> list[dict]:
    result = []
    for m in messages:
        if isinstance(m, HumanMessage):
            result.append({"role": "consultant", "content": m.content})
        elif isinstance(m, AIMessage):
            result.append({"role": "client", "content": m.content})
        elif isinstance(m, dict):
            role = m.get("type", m.get("role", "unknown"))
            label = "consultant" if role == "human" else "client"
            result.append({"role": label, "content": m.get("content", "")})
    return result


def _serialize_revealed_items(revealed_items: list) -> list[dict]:
    result = []
    for item in revealed_items:
        if hasattr(item, "id"):
            result.append({
                "id": item.id,
                "content": item.content,
                "layer": item.layer,
                "topic": getattr(item, "topic", ""),
            })
        elif isinstance(item, dict):
            result.append(item)
    return result


def _compute_summary_stats(annotations: list) -> dict:
    total = len(annotations)

    # Turn type counts.
    by_type: dict[str, int] = {}
    for a in annotations:
        tt = a.get("turn_type", "question")
        by_type[tt] = by_type.get(tt, 0) + 1

    # Question-only stats (is_well_formed/information_elicited can be None for other types).
    questions = [a for a in annotations if a.get("turn_type", "question") == "question"]
    q_total = len(questions)
    q_with_mistakes = sum(1 for a in questions if a.get("is_well_formed") is False)
    q_well_formed = q_total - q_with_mistakes
    q_elicited = sum(1 for a in questions if a.get("information_elicited") is True)
    q_not_elicited = q_total - q_elicited
    q_well_formed_no_info = sum(
        1 for a in questions
        if a.get("is_well_formed") is True and a.get("information_elicited") is False
    )

    mistake_counts: dict[str, int] = {}
    for ann in questions:
        for m in ann.get("mistakes", []):
            mt = m.get("mistake_type", "unknown")
            mistake_counts[mt] = mistake_counts.get(mt, 0) + 1

    return {
        "total_turns": total,
        "turns_by_type": by_type,
        "questions_total": q_total,
        "questions_well_formed": q_well_formed,
        "questions_with_mistakes": q_with_mistakes,
        "questions_information_elicited": q_elicited,
        "questions_no_information_elicited": q_not_elicited,
        "questions_well_formed_no_info": q_well_formed_no_info,
        "mistake_type_frequencies": dict(
            sorted(mistake_counts.items(), key=lambda x: -x[1])
        ),
    }


def save_session(
    scenario_title: str,
    transcript: list,
    revealed_items: list,
    eval_state: dict,
) -> Path:
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.now()
    filename = logs_dir / f"session_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.json"

    annotations = eval_state.get("turn_annotations", [])

    payload = {
        "timestamp": timestamp.isoformat(),
        "scenario": scenario_title,
        "transcript": _serialize_messages(transcript),
        "revealed_items": _serialize_revealed_items(revealed_items),
        "turn_annotations": annotations,
        "simulated_alternatives": eval_state.get("simulated_alternatives", []),
        "report": eval_state.get("report", ""),
        "summary_stats": _compute_summary_stats(annotations),
    }

    filename.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return filename
