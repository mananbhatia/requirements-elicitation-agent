"""
Terminal conversation loop.

Usage:
  python main.py                                          # uses default scenario
  python main.py docs/scenarios/waste_management_client.md

LangGraph Concept: INVOKE vs STREAM
=====================================
graph.invoke(state) — runs the graph to completion, returns final state.
graph.stream(state) — yields state snapshots after each node finishes.

We use invoke() here. State (messages + revealed_items) is held in Python
between turns and passed in fresh each call — "in-memory" state management.
"""

import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage
from knowledge import load_scenario
from graph import build_graph

DEFAULT_SCENARIO = Path(__file__).parent / "docs" / "scenarios" / "waste_management_client.md"


def run(scenario_path: str | Path = DEFAULT_SCENARIO):
    scenario = load_scenario(scenario_path)
    graph = build_graph(scenario)

    print("\n" + "=" * 60)
    print(f"  Revodata Synthetic Client Interview")
    print(f"  Scenario: {scenario.title}")
    print(f"  Tacit knowledge items: {len(scenario.tacit_items)}")
    print("=" * 60)
    print("\nType your questions as the consultant.")
    print("Type 'done' or 'exit' to end the interview.\n")

    # Kick off with a hidden system prompt asking the client to open naturally.
    opening_prompt = HumanMessage(
        content="[Start of interview. Introduce yourself and state your opening requirement in 2-3 sentences. Be natural and conversational.]"
    )
    state = graph.invoke({"messages": [opening_prompt], "revealed_items": []})

    client_opening = state["messages"][-1].content
    print(f"Client: {client_opening}\n")

    # Drop the fake opening prompt; carry forward only the client's first line.
    messages = [state["messages"][-1]]
    revealed_items = state.get("revealed_items", [])

    while True:
        try:
            consultant_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n[Interview ended]")
            break

        if not consultant_input:
            continue

        if consultant_input.lower() in ("done", "exit", "quit"):
            print("\n[Interview ended. Evaluation coming in a future version.]\n")
            break

        messages.append(HumanMessage(content=consultant_input))
        state = graph.invoke({"messages": messages, "revealed_items": revealed_items})

        client_response = state["messages"][-1].content
        print(f"\nClient: {client_response}\n")

        messages = state["messages"]
        revealed_items = state.get("revealed_items", [])


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENARIO
    run(path)
