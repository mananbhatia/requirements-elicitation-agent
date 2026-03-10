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

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from state import ConversationState
from knowledge import Scenario, ScenarioItem, retrieve_relevant_knowledge

# Generic behavior rules — no scenario-specific names or details.
# Character-specific behavior (team deference, personality, maturity) lives
# in the scenario file's own instructions section.
_BEHAVIOR_RULES = """
## RULES — apply to every response without exception

1. ANSWER ONLY WHAT WAS ASKED. Stop there. Do not add context, background, or
   related information. If there are five things you could say, say the most
   relevant one and stop.

2. NEVER VOLUNTEER INFORMATION. You do not summarise, compile, or give overviews.
   The consultant builds the full picture by asking.

3. YOU ONLY KNOW WHAT IS IN YOUR CONTEXT. If a fact is not explicitly in your
   character description or revealed facts, you do not know it — including details
   about systems that are merely named but not described. When you don't know
   something: say so. Never fill a gap with a plausible guess.

4. NEVER GIVE RECOMMENDATIONS OR PRIORITIES. That is the consultant's job.
   If asked what to prioritise, where to start, or what you would do: redirect —
   "that's what I need your help figuring out" — and stop.

5. NEVER BREAK CHARACTER. Never acknowledge being an AI or a simulation.

6. Respond naturally and conversationally. Avoid technical terminology, structured
   formatting, and excessive detail. Keep responses concise.

7. For vague or broad questions: give a minimal, non-specific response and ask
   what they want to focus on.

8. Only ask a question when you genuinely don't understand what was said or
   proposed. Never ask questions to hand control back to the consultant.
"""


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

## FACTS YOU CAN NOW REFERENCE

The consultant asked specifically enough to surface the following.
You may reference these naturally if they come up again, but do not
volunteer further related details that weren't asked about.

{injected}
"""
    return prompt


def build_nodes(scenario: Scenario):
    """
    Returns (retrieval_node, client_node) as closures over the loaded scenario.
    Passing a different Scenario object produces a completely different client.
    """
    llm = ChatOpenAI(model="gpt-4o", temperature=0.7)

    def retrieval_node(state: ConversationState) -> dict:
        """
        Runs first each turn. Reads the consultant's latest message,
        checks it against unrevealed tacit items, and returns any newly unlocked ones.
        """
        last_human = next(
            (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
            None,
        )
        if last_human is None:
            return {"revealed_items": []}

        already_revealed_ids = [item["id"] for item in state.get("revealed_items", [])]
        newly_revealed = retrieve_relevant_knowledge(
            last_human.content,
            scenario.surface_items,
            scenario.tacit_items,
            already_revealed_ids,
        )
        # Convert dataclasses to dicts for storage in state.
        return {"revealed_items": [vars(item) for item in newly_revealed]}

    def client_node(state: ConversationState) -> dict:
        """
        Builds the system prompt from surface text + all revealed tacit items so far,
        then calls the LLM. The model cannot leak what isn't in the prompt.
        """
        revealed_items = [
            ScenarioItem(**item) for item in state.get("revealed_items", [])
        ]
        system_prompt = _build_system_prompt(scenario.character_text, revealed_items)
        messages_with_system = [SystemMessage(content=system_prompt)] + state["messages"]
        response = llm.invoke(messages_with_system)
        return {"messages": [response]}

    return retrieval_node, client_node
