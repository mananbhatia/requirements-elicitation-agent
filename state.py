"""
LangGraph Concept: STATE
========================
State is the shared data structure that flows through every node in the graph.
Each node reads from it and returns a partial update — LangGraph merges those
updates back using "reducers" defined per field.

We define two fields:

1. messages — the conversation history.
   Reducer: add_messages (appends new messages, never overwrites)

2. revealed_items — tacit knowledge items unlocked so far.
   Reducer: custom function that merges by ID (so re-revealing the same item
   across multiple turns doesn't create duplicates).

Why track revealed_items in state?
  - The retrieval_node adds to it each turn when new knowledge is unlocked.
  - The client_node reads it to build the system prompt for that turn.
  - It persists across turns so the client "remembers" what they've already told the consultant.
"""

from typing import Annotated, TypedDict
from langgraph.graph import add_messages


def _merge_revealed(existing: list[dict], new: list[dict]) -> list[dict]:
    """Append only items whose ID isn't already in the list."""
    existing_ids = {item["id"] for item in existing}
    return existing + [item for item in new if item["id"] not in existing_ids]


class ConversationState(TypedDict):
    messages: Annotated[list, add_messages]
    revealed_items: Annotated[list[dict], _merge_revealed]
