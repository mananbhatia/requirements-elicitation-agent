"""
LangGraph Concept: NODES
========================
A node is a Python function: (state) -> dict of updates.
We have two nodes per turn:

1. retrieval_node  — checks the consultant's question, unlocks tacit knowledge if earned
2. client_node     — builds a dynamic system prompt and calls the LLM

Both nodes are created as closures inside build_nodes() so they close over the
loaded Scenario object. This means the same node logic works for any scenario —
swap the scenario file, get a completely different synthetic client.
"""

from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from state import ConversationState
from knowledge import Scenario, ScenarioItem, retrieve_relevant_knowledge

# Generic behavior rules — loaded from docs/behavior_rules.md.
# Kept in markdown so they can be edited without touching code.
# Character-specific behavior (team deference, personality, maturity) lives
# in the scenario file's own sections.
_BEHAVIOR_RULES = (
    Path(__file__).parent / "docs" / "behavior_rules.md"
).read_text()


def _build_system_prompt(character_text: str, revealed_items: list[ScenarioItem]) -> str:
    """
    Construct the system prompt for this turn.
    character_text is always present — it defines identity, personality, and limitations
    but contains NO factual data points. Facts are injected only as they are revealed.
    """
    prompt = _BEHAVIOR_RULES + "\n\n## YOUR CHARACTER\n\n" + character_text

    if revealed_items:
        injected = "\n".join(f"- {item.content}" for item in revealed_items)
        prompt += f"""

## WHAT YOU KNOW ABOUT THE SITUATION

{injected}

Speak from this knowledge naturally when it's relevant to what's being discussed.
Do not restate these points verbatim — put them in your own words and experience.
"""

    prompt += "\n\nRespond in 2-4 sentences. No em-dashes (—).\n"

    return prompt


def build_nodes(scenario: Scenario):
    """
    Returns (retrieval_node, client_node) as closures over the loaded scenario.
    Passing a different Scenario object produces a completely different client.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.7)

    def retrieval_node(state: ConversationState) -> dict:
        """
        Runs first each turn. Reads the consultant's latest message,
        checks it against unrevealed items, and returns any newly unlocked ones.
        Passes recent conversation context so the retrieval LLM can resolve
        pronouns and follow-up references (e.g. "is it X or Y?" after discussing a topic).
        """
        messages = state["messages"]
        last_human = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)),
            None,
        )
        if last_human is None:
            return {"revealed_items": []}

        # Build recent context from last 2 turns (human + AI pairs).
        recent = messages[-4:] if len(messages) >= 4 else messages
        context_lines = []
        for m in recent:
            if isinstance(m, HumanMessage):
                context_lines.append(f"Consultant: {m.content}")
            else:
                context_lines.append(f"Client: {m.content}")
        recent_context = "\n".join(context_lines)

        already_revealed_ids = [item["id"] for item in state.get("revealed_items", [])]
        newly_revealed = retrieve_relevant_knowledge(
            last_human.content,
            scenario.surface_items,
            scenario.tacit_items,
            already_revealed_ids,
            recent_context=recent_context,
        )
        # Current consultant turn index (1-based, excluding the hidden opening prompt).
        turn_index = sum(
            1 for m in messages
            if isinstance(m, HumanMessage)
            and not m.content.startswith("[Start of interview")
        )
        # Convert dataclasses to dicts and stamp which turn unlocked each item.
        return {"revealed_items": [
            {**vars(item), "unlocked_at_turn": turn_index}
            for item in newly_revealed
        ]}

    def client_node(state: ConversationState) -> dict:
        """
        Builds the system prompt from surface text + all revealed tacit items so far,
        then calls the LLM. The model cannot leak what isn't in the prompt.
        """
        revealed_items = [
            ScenarioItem(
                id=item["id"],
                content=item["content"],
                layer=item["layer"],
                topic=item.get("topic", ""),
            )
            for item in state.get("revealed_items", [])
        ]
        system_prompt = _build_system_prompt(scenario.character_text, revealed_items)
        messages_with_system = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm.invoke(messages_with_system)
        return {"messages": [response]}

    return retrieval_node, client_node
