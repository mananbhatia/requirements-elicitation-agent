"""
LangGraph nodes for the embedding-based retrieval system.

Design: retrieval_node is a no-op pass-through kept for graph shape compatibility
with alternative_simulator.py, which invokes the conversation graph as a black box.
All retrieval + prompt construction happens in client_node because:
  - CK retrieval is per-turn context (not stored in state) — must happen at prompt build time
  - Embedding retrieval is fast (in-memory dot product after one API call per session)
  - client_node returns messages, revealed_items, and retrieval_traces in one shot
"""

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

from state import ConversationState
from knowledge import (
    Scenario,
    ScenarioItem,
    EmbeddingStore,
    retrieve_relevant_knowledge,
)
from paths import BEHAVIOR_RULES_FILE

_BEHAVIOR_RULES = BEHAVIOR_RULES_FILE.read_text()


def _build_system_prompt(
    character_text: str,
    character_paragraphs: list[ScenarioItem],
    all_revealed_discovery: list[ScenarioItem],
) -> str:
    """
    Construct the system prompt for one turn.

    character_text       — always present (identity, personality, team, maturity).
                           Contains NO factual platform data.
    character_paragraphs — CK items retrieved fresh this turn as topical context.
                           Provides background so the client can speak naturally about
                           the area being discussed without revealing it unprompted.
    all_revealed_discovery — all DI items disclosed so far (prior turns + this turn).
                             These are specific facts the client can now mention.
    """
    prompt = _BEHAVIOR_RULES + "\n\n## YOUR CHARACTER\n\n" + character_text

    if character_paragraphs:
        context_text = "\n\n".join(item.content for item in character_paragraphs)
        prompt += f"\n\n## BACKGROUND CONTEXT RELEVANT TO THIS TOPIC\n\n{context_text}"

    if all_revealed_discovery:
        details = "\n".join(f"- {item.content}" for item in all_revealed_discovery)
        prompt += f"""

## WHAT YOU KNOW ABOUT THE SITUATION

{details}

Speak from this knowledge naturally when it's relevant to what's being discussed.
Do not restate these points verbatim — put them in your own words and experience.
"""

    prompt += "\n\nRespond in 2-4 sentences. No em-dashes (—).\n"
    return prompt


def build_nodes(scenario: Scenario, char_index: EmbeddingStore, disc_index: EmbeddingStore):
    """
    Returns (retrieval_node, client_node) as closures over scenario + embedding indices.

    char_index and disc_index should be built once with build_retrieval_index(scenario)
    and reused for all turns in the session.
    """
    llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.7)

    def retrieval_node(state: ConversationState) -> dict:
        """No-op pass-through. All retrieval is handled in client_node."""
        return {}

    def client_node(state: ConversationState) -> dict:
        """
        1. Finds the latest consultant question.
        2. Calls retrieve_relevant_knowledge → (CK paragraphs, new DI items, trace).
        3. Builds system prompt with character_text + CK context + all revealed DI facts.
        4. Calls the LLM.
        5. Returns message update, newly revealed DI items, and retrieval trace for state.
        """
        messages = state["messages"]
        last_human = next(
            (m for m in reversed(messages) if isinstance(m, HumanMessage)),
            None,
        )

        # No question yet (e.g. initial graph invocation with only system messages)
        if last_human is None:
            revealed_dicts = state.get("revealed_items", [])
            all_revealed = [
                ScenarioItem(id=d["id"], content=d["content"], topic=d.get("topic", ""))
                for d in revealed_dicts
            ]
            system_prompt = _build_system_prompt(scenario.character_text, [], all_revealed)
            response = llm.invoke([SystemMessage(content=system_prompt)] + messages)
            return {"messages": [response], "retrieval_traces": []}

        # Build preceding context for context-aware retrieval:
        # last exchange BEFORE the current question (1 consultant/client pair)
        preceding = messages[-3:-1] if len(messages) >= 3 else messages[:-1]
        context_lines = []
        for m in preceding:
            if isinstance(m, HumanMessage):
                context_lines.append(f"Consultant: {m.content}")
            else:
                context_lines.append(f"Client: {m.content}")
        recent_context = "\n".join(context_lines)

        already_revealed_ids = [item["id"] for item in state.get("revealed_items", [])]

        char_paragraphs, new_disc_items, trace = retrieve_relevant_knowledge(
            last_human.content,
            char_index,
            disc_index,
            scenario,
            already_revealed_ids,
            recent_context=recent_context,
        )

        # Current turn index (1-based, skipping the hidden opening prompt)
        turn_index = sum(
            1 for m in messages
            if isinstance(m, HumanMessage)
            and not m.content.startswith("[Start of interview")
        )

        # All previously revealed DI items (from state) + newly revealed ones
        prev_revealed = [
            ScenarioItem(id=d["id"], content=d["content"], topic=d.get("topic", ""))
            for d in state.get("revealed_items", [])
        ]
        all_revealed_disc = prev_revealed + new_disc_items

        system_prompt = _build_system_prompt(
            scenario.character_text,
            char_paragraphs,
            all_revealed_disc,
        )
        messages_with_system = [SystemMessage(content=system_prompt)] + messages
        response = llm.invoke(messages_with_system)

        # Persist newly unlocked DI items to state — canonical schema: id, content, topic, unlocked_at_turn
        new_revealed_dicts = [
            {"id": item.id, "content": item.content, "topic": item.topic, "unlocked_at_turn": turn_index}
            for item in new_disc_items
        ]

        # Attach turn metadata to trace. Skip for turn_index == 0 (opening prompt —
        # no real consultant question, pre-filters will block it anyway).
        if turn_index > 0:
            trace["turn_index"] = turn_index
            trace["consultant_question"] = last_human.content
            new_traces = [trace]
        else:
            new_traces = []

        return {"messages": [response], "revealed_items": new_revealed_dicts, "retrieval_traces": new_traces}

    return retrieval_node, client_node
