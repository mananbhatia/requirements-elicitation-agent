"""
LangGraph Concept: THE GRAPH
=============================
A StateGraph is the container. You add nodes and edges to define the flow.

Edges come in two types:
  - Normal edges:      A -> B  (always go from A to B)
  - Conditional edges: A -> (B or C depending on state)  ← we'll use this later

Special sentinels:
  START  — the entry point. Where execution begins each time you .invoke()
  END    — terminates the graph for this invocation.

.compile() validates the graph (no dangling nodes, no missing edges) and
returns a CompiledGraph that you can call .invoke() or .stream() on.

Our graph for now is trivially simple:
  START -> synthetic_client_node -> END

Each call to graph.invoke() is one conversation turn.
The state (messages list) accumulates across turns because we pass the
updated state back into the next invoke() call from main.py.
"""

from langgraph.graph import StateGraph, START, END
from state import ConversationState
from client import synthetic_client_node


def build_graph():
    builder = StateGraph(ConversationState)

    # Add nodes — just the synthetic client for now.
    builder.add_node("synthetic_client", synthetic_client_node)

    # Wire up edges.
    builder.add_edge(START, "synthetic_client")
    builder.add_edge("synthetic_client", END)

    return builder.compile()


# Build once and export — main.py imports this.
graph = build_graph()
