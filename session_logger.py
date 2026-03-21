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
    with_mistakes = sum(1 for a in annotations if not a.get("is_well_formed", True))
    well_formed = total - with_mistakes
    elicited = sum(1 for a in annotations if a.get("information_elicited", True))
    not_elicited = total - elicited
    well_formed_no_info = sum(
        1 for a in annotations
        if a.get("is_well_formed", True) and not a.get("information_elicited", True)
    )

    mistake_counts: dict[str, int] = {}
    for ann in annotations:
        for m in ann.get("mistakes", []):
            mt = m.get("mistake_type", "unknown")
            mistake_counts[mt] = mistake_counts.get(mt, 0) + 1

    return {
        "total_turns": total,
        "well_formed": well_formed,
        "with_mistakes": with_mistakes,
        "information_elicited": elicited,
        "no_information_elicited": not_elicited,
        "well_formed_no_info": well_formed_no_info,
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
