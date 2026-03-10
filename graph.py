"""
LangGraph Concept: THE GRAPH
=============================
build_graph() now accepts a Scenario object and wires it into the nodes via closures.
Swapping scenarios requires no code changes — just pass a different Scenario.

Flow each turn:
  START → retrieval_node → client_node → END
"""

from langgraph.graph import StateGraph, START, END
from state import ConversationState
from knowledge import Scenario
from client import build_nodes


def build_graph(scenario: Scenario):
    retrieval_node, client_node = build_nodes(scenario)

    builder = StateGraph(ConversationState)
    builder.add_node("retrieval", retrieval_node)
    builder.add_node("client", client_node)
    builder.add_edge(START, "retrieval")
    builder.add_edge("retrieval", "client")
    builder.add_edge("client", END)

    return builder.compile()
