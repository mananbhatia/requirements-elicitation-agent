"""
Terminal conversation loop.

LangGraph Concept: INVOKE vs STREAM
=====================================
graph.invoke(state) — runs the graph to completion, returns final state.
graph.stream(state) — yields state snapshots after each node finishes.

We use invoke() here: pass in the current messages, get back the updated
messages list including Danny's new response.

Key pattern: we own the state between turns.
LangGraph doesn't persist state automatically in this setup — we hold the
`messages` list ourselves and pass it in on each invoke(). This is called
"in-memory" state. Later, with checkpointers, LangGraph can persist state
to a DB and resume across sessions.
"""

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage
from graph import graph


def run():
    print("\n" + "=" * 60)
    print("  Revodata Synthetic Client Interview")
    print("  Scenario: GreenCycle Industries — Access Control")
    print("=" * 60)
    print("\nType your questions as the consultant.")
    print("Type 'done' or 'exit' to end the interview.\n")

    # Danny opens with the initial brief — a one-liner to kick things off.
    # We invoke the graph with no prior messages so Danny introduces himself.
    opening_prompt = HumanMessage(
        content="[Start of interview. Please introduce yourself and state your opening requirement in 2-3 sentences. Be natural.]"
    )
    state = graph.invoke({"messages": [opening_prompt], "revealed_items": []})

    # Print Danny's opening, then drop the fake prompt from history so the
    # consultant's first real turn is what goes on record.
    danny_opening = state["messages"][-1].content
    print(f"Danny: {danny_opening}\n")

    # Carry forward Danny's opening line and the revealed_items state.
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

        # Add consultant's message and invoke the graph.
        messages.append(HumanMessage(content=consultant_input))
        state = graph.invoke({"messages": messages, "revealed_items": revealed_items})

        # The last message is Danny's response.
        danny_response = state["messages"][-1].content
        print(f"\nDanny: {danny_response}\n")

        # Accumulate full history and revealed knowledge for next turn.
        messages = state["messages"]
        revealed_items = state.get("revealed_items", [])


if __name__ == "__main__":
    run()
