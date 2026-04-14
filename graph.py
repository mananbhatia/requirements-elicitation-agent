"""
LangGraph conversation graph.

Build the embedding indices once here so they are reused for all turns in the session.

Usage:
    from knowledge import load_scenario, build_retrieval_index
    from graph import build_graph

    scenario = load_scenario("docs/scenarios/waste_management.md", persona="Danny")
    graph = build_graph(scenario)
    result = graph.invoke({"messages": [...], "revealed_items": [], "retrieval_traces": []})
"""

from langgraph.graph import StateGraph, START, END
from state import ConversationState
from knowledge import Scenario, build_retrieval_index
from client import build_nodes


def build_graph(scenario: Scenario):
    """
    Compile the conversation graph for a loaded scenario.

    Builds embedding indices from scenario.character_knowledge and scenario.discovery_items.
    Indices are built once and shared across all turns via closure.
    """
    char_index, disc_index = build_retrieval_index(scenario)
    retrieval_node, client_node = build_nodes(scenario, char_index, disc_index)

    builder = StateGraph(ConversationState)
    builder.add_node("retrieval", retrieval_node)
    builder.add_node("client", client_node)
    builder.add_edge(START, "retrieval")
    builder.add_edge("retrieval", "client")
    builder.add_edge("client", END)

    return builder.compile()
