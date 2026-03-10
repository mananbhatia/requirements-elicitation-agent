"""
LangGraph Concept: THE GRAPH
=============================
We now have two nodes wired in sequence:

  START → retrieval_node → client_node → END

Each turn:
  1. retrieval_node checks the consultant's question, unlocks tacit knowledge if earned.
  2. client_node builds a fresh system prompt with only what's been revealed so far,
     then calls the LLM.

The key insight: client_node never sees tacit knowledge it hasn't earned.
The LLM cannot leak information that isn't in its context.
"""

from langgraph.graph import StateGraph, START, END
from state import ConversationState
from client import retrieval_node, client_node


def build_graph():
    builder = StateGraph(ConversationState)

    builder.add_node("retrieval", retrieval_node)
    builder.add_node("client", client_node)

    builder.add_edge(START, "retrieval")
    builder.add_edge("retrieval", "client")
    builder.add_edge("client", END)

    return builder.compile()


graph = build_graph()
