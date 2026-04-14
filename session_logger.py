"""
Session logger — saves complete session data after each evaluation.

Local development: writes JSON to SESSION_LOG_DIR (defaults to logs/) using
standard filesystem calls.

Databricks Apps deployment: when SESSION_LOG_DIR starts with /Volumes/,
writes via the Databricks Files API — Unity Catalog Volumes are not mounted
as a regular filesystem in the App container.

Returns (path, content) so callers have the JSON without reading it back.
"""

import json
import os
import requests
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, AIMessage
from paths import SESSION_LOG_DIR


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

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
    """Serialize revealed DI items to canonical schema: id, content, topic, unlocked_at_turn."""
    result = []
    for item in revealed_items:
        if hasattr(item, "id"):
            entry = {
                "id": item.id,
                "content": item.content,
                "topic": getattr(item, "topic", ""),
            }
        elif isinstance(item, dict):
            entry = {
                "id": item.get("id", ""),
                "content": item.get("content", ""),
                "topic": item.get("topic", ""),
            }
            if "unlocked_at_turn" in item:
                entry["unlocked_at_turn"] = item["unlocked_at_turn"]
        else:
            continue
        result.append(entry)
    return result


def _compute_summary_stats(annotations: list) -> dict:
    total = len(annotations)

    by_type: dict[str, int] = {}
    for a in annotations:
        tt = a.get("turn_type", "question")
        by_type[tt] = by_type.get(tt, 0) + 1

    questions = [a for a in annotations if a.get("turn_type", "question") == "question"]
    q_total = len(questions)
    q_with_mistakes = sum(1 for a in questions if a.get("is_well_formed") is False)
    q_well_formed = q_total - q_with_mistakes

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
        "mistake_type_frequencies": dict(
            sorted(mistake_counts.items(), key=lambda x: -x[1])
        ),
    }


# ---------------------------------------------------------------------------
# Databricks Files API helpers (used when SESSION_LOG_DIR is a Volume path)
# ---------------------------------------------------------------------------

def _get_workspace_host() -> str:
    """
    Returns the Databricks workspace base URL with https:// scheme.
    DATABRICKS_HOST is auto-injected in Databricks Apps (hostname only, no scheme).
    Falls back to deriving from DATABRICKS_BASE_URL for local testing.
    """
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if not host:
        host = os.environ.get("DATABRICKS_BASE_URL", "").rstrip("/").removesuffix("/serving-endpoints")
    if host and not host.startswith("https://"):
        host = f"https://{host}"
    return host



def _files_api_write(file_path: Path, content: str, host: str, token: str) -> None:
    """Write a file to a Unity Catalog Volume via the Databricks Files API."""
    api_path = str(file_path).lstrip("/")
    resp = requests.put(
        f"{host}/api/2.0/fs/files/{api_path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
        data=content.encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_partial_session(
    scenario_title: str,
    transcript: list,
    revealed_items: list,
    session_id: str,
    consultant_email: str = "unknown",
    retrieval_traces: list | None = None,
) -> None:
    """
    Save transcript and revealed items mid-conversation, before evaluation runs.

    Uses a fixed filename keyed by session_id so it overwrites on each turn
    rather than accumulating files. The full save_session() call at evaluation
    end writes a separate timestamped file.

    Silently ignores write errors — partial saves are best-effort.
    """
    logs_dir = SESSION_LOG_DIR
    filename = logs_dir / f"partial_{session_id}.json"

    payload = {
        "partial": True,
        "session_id": session_id,
        "consultant_email": consultant_email,
        "scenario": scenario_title,
        "transcript": _serialize_messages(transcript),
        "revealed_items": _serialize_revealed_items(revealed_items),
        "retrieval_traces": retrieval_traces or [],
    }
    content = json.dumps(payload, indent=2, ensure_ascii=False)

    try:
        if str(logs_dir).startswith("/Volumes/"):
            host = _get_workspace_host()
            token = os.environ.get("DATABRICKS_TOKEN", "")
            _files_api_write(filename, content, host, token)
        else:
            logs_dir.mkdir(parents=True, exist_ok=True)
            filename.write_text(content)
    except Exception:
        pass  # partial saves are best-effort


def save_session(
    scenario_title: str,
    transcript: list,
    revealed_items: list,
    eval_state: dict,
    consultant_email: str = "unknown",
    retrieval_traces: list | None = None,
) -> tuple[Path, str]:
    """
    Saves session data and returns (path, json_content).

    Dispatches on SESSION_LOG_DIR:
    - /Volumes/...  → Databricks Files API (App deployment)
    - anything else → standard filesystem (local development)
    """
    logs_dir = SESSION_LOG_DIR
    timestamp = datetime.now()
    filename = logs_dir / f"session_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.json"

    annotations = eval_state.get("turn_annotations", [])
    payload = {
        "timestamp": timestamp.isoformat(),
        "consultant_email": consultant_email,
        "scenario": scenario_title,
        "transcript": _serialize_messages(transcript),
        "revealed_items": _serialize_revealed_items(revealed_items),
        "turn_annotations": annotations,
        "simulated_alternatives": eval_state.get("simulated_alternatives", []),
        "report": eval_state.get("report", {}),
        "summary_stats": _compute_summary_stats(annotations),
        "retrieval_traces": retrieval_traces or [],
    }
    content = json.dumps(payload, indent=2, ensure_ascii=False)

    if str(logs_dir).startswith("/Volumes/"):
        host = _get_workspace_host()
        token = os.environ.get("DATABRICKS_TOKEN", "")
        _files_api_write(filename, content, host, token)
    else:
        logs_dir.mkdir(parents=True, exist_ok=True)
        filename.write_text(content)

    return filename, content
