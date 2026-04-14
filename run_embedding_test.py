"""
Quick smoke-test for the embedding-based retrieval system.

Run from the agent_v2/ directory:
    python run_embedding_test.py

Requires: ANTHROPIC_API_KEY and VOYAGE_API_KEY in environment (or .env file).
"""

import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Enable DEBUG logging for retrieval scoring — this is the main thing to inspect.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
# Suppress noisy third-party loggers; keep only our retrieval logger verbose.
for noisy in ("httpx", "httpcore", "anthropic", "langchain", "langgraph",
              "voyageai", "urllib3", "urllib3.connectionpool"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from langchain_core.messages import HumanMessage

from knowledge import load_scenario
from graph import build_graph

SCENARIO = Path("docs/scenarios/waste_management.md")
PERSONA = "Danny"

# Representative turns: mix of gate failures, specific questions, and follow-ups.
TURNS = [
    # Should be blocked by intent_check (catch-all)
    "Is there anything else you'd like to share?",
    # Specific question — should unlock a DI item about user provisioning
    "How are users added to the Databricks platform — is that done automatically or manually?",
    # Specific question — should unlock DI items about network/endpoint exposure
    "Is your Databricks environment accessible over the public internet, or is it on a private network?",
    # Follow-up (referential "it") — should trigger context-aware retrieval
    "Is it locked down to your internal network at all?",
    # Broad access-control question — should unlock DI items, maybe CK context too
    "How is access to data controlled — who can see what, and how is that managed?",
]


def run():
    print(f"\n{'='*70}")
    print(f"Loading scenario: {SCENARIO} — persona: {PERSONA}")
    scenario = load_scenario(str(SCENARIO), persona=PERSONA)
    print(f"  CK items: {len(scenario.character_knowledge)}")
    print(f"  DI items: {len(scenario.discovery_items)}")

    print("\nBuilding graph (embedding index construction)...")
    graph = build_graph(scenario)
    print("  Done.")

    state = {
        "messages": [
            HumanMessage(content="[Start of interview] Hello, thanks for meeting with me today.")
        ],
        "revealed_items": [],
    }
    # Run the opening turn silently (no consultant question yet)
    state = graph.invoke(state)

    for i, question in enumerate(TURNS, 1):
        print(f"\n{'='*70}")
        print(f"TURN {i}: {question!r}")
        print("-" * 70)

        state["messages"].append(HumanMessage(content=question))
        state = graph.invoke(state)

        response = state["messages"][-1].content
        revealed_ids = [item["id"] for item in state.get("revealed_items", [])]

        print(f"\nCLIENT: {response}")
        print(f"\nRevealed DI IDs so far: {revealed_ids or '(none)'}")


if __name__ == "__main__":
    run()
