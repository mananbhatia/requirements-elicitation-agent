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
from eval_graph import build_eval_graph
from session_logger import save_session

DEFAULT_SCENARIO = Path(__file__).parent / "docs" / "scenarios" / "waste_management_client.md"


def _run_evaluation(messages, revealed_items, scenario, graph, scenario_title):
    try:
        answer = input("Run evaluation? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer != "y":
        return

    eval_graph = build_eval_graph(graph)
    eval_state = eval_graph.invoke({
        "transcript": messages,
        "revealed_items": revealed_items,
        "turn_annotations": [],
        "simulated_alternatives": [],
        "report": "",
    })

    annotations = eval_state.get("turn_annotations", [])
    alternatives = {
        a["turn_index"]: a
        for a in eval_state.get("simulated_alternatives", [])
    }

    print("=" * 60)
    print("  EVALUATION — Turn-by-turn analysis")
    print("=" * 60)

    if not annotations:
        print("No consultant turns found to evaluate.\n")
        return

    for ann in annotations:
        idx = ann.get("turn_index", "?")
        question = ann.get("question", "")
        mistakes = ann.get("mistakes", [])
        well_formed = ann.get("is_well_formed", True)
        info_elicited = ann.get("information_elicited", True)

        print(f"\nTurn {idx}: {question!r}")
        print(f"Well-formed: {'yes' if well_formed else 'no'} | Information elicited: {'yes' if info_elicited else 'no'}")
        if mistakes:
            for m in mistakes:
                print(f"  - [{m['mistake_type']}] {m['explanation']}")
        else:
            print("  No mistakes found.")

        if idx in alternatives:
            alt = alternatives[idx]
            print(f"  Original response: {alt['original_response']}")
            print(f"  Alternative: {alt['alternative_question']}")
            print(f"  Simulated response: {alt['simulated_response']}")
    print()

    report = eval_state.get("report", "")
    if report:
        print("=" * 60)
        print("  FEEDBACK REPORT")
        print("=" * 60)
        print()
        print(report)
        print()

    log_path = save_session(scenario_title, messages, revealed_items, eval_state)
    print(f"[LOG] Session saved to {log_path}")


def run(scenario_path: str | Path = DEFAULT_SCENARIO):
    scenario = load_scenario(scenario_path)
    graph = build_graph(scenario)

    print("\n" + "=" * 60)
    print(f"  Revodata — Consultant Interview Training")
    print("=" * 60)
    print(f"""
BRIEFING
--------
Client:       {scenario.title}
Meeting type: Initial discovery — first meeting, no prior work done
Your role:    Lead the discovery. Ask questions.

The client is looking for help with their Databricks setup.
Conduct the interview as you would with a real client.
Type 'done' or 'exit' when finished.
""")

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
            print("\n\n[Interview ended.]")
            _run_evaluation(messages, revealed_items, scenario, graph, scenario.title)
            break

        if not consultant_input:
            continue

        if consultant_input.lower() in ("done", "exit", "quit"):
            print("\n[Interview ended.]\n")
            _run_evaluation(messages, revealed_items, scenario, graph, scenario.title)
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
