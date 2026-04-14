"""
LangGraph Concept: STATE
========================
State is the shared data structure that flows through every node in the graph.
Each node reads from it and returns a partial update — LangGraph merges those
updates back using "reducers" defined per field.

Fields:

1. messages — the conversation history.
   Reducer: add_messages (appends new messages, never overwrites)

2. revealed_items — discovery items (DI) unlocked so far.
   Reducer: custom function that merges by ID (no duplicates across turns).
   Each entry: {id, content, topic, unlocked_at_turn}
   client_node reads this to re-inject all known facts into the system prompt.

3. retrieval_traces — one trace dict per real consultant turn.
   Reducer: append-only, None-safe (simulation invocations omit this field).
   Written to session JSON logs for offline inspection and threshold tuning.
"""

from typing import Annotated, TypedDict
from langgraph.graph import add_messages


def _merge_revealed(existing: list[dict], new: list[dict]) -> list[dict]:
    """Append only items whose ID isn't already in the list."""
    existing_ids = {item["id"] for item in existing}
    return existing + [item for item in new if item["id"] not in existing_ids]


def _append_traces(existing: list | None, new: list | None) -> list:
    """
    Append-only reducer for per-turn retrieval traces.
    Handles None so alternative_simulator.py simulation invocations (which don't
    include retrieval_traces in their invoke dict) work without modification.
    """
    return (existing or []) + (new or [])


class ConversationState(TypedDict):
    messages: Annotated[list, add_messages]
    revealed_items: Annotated[list[dict], _merge_revealed]
    retrieval_traces: Annotated[list[dict], _append_traces]
