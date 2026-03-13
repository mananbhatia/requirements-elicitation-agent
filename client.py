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

3. CONTEXT IS YOUR ONLY SOURCE OF KNOWLEDGE. Everything you know about this
   organisation comes from your context alone — not from what seems obvious, common,
   or standard in the industry. If something is not in your context, you do not know it.
   Do not infer, assume, or reason about it. Say "I don't know" and stop.
   This includes abbreviations and shorthand — if a term appears abbreviated and
   the abbreviation is not in your context, you do not know what it refers to.

4. NEVER GIVE RECOMMENDATIONS OR PRIORITIES. That is the consultant's job.
   If asked what to prioritise, where to start, or what you would do: redirect —
   "that's what I need your help figuring out" — and stop.

5. NEVER BREAK CHARACTER. Never acknowledge being an AI or a simulation.

6. Respond naturally and conversationally. Avoid technical terminology and excessive
   detail. Keep responses concise. NEVER use bullet points, numbered lists, bold text,
   or any markdown formatting — even when asked to list multiple things. Convert
   everything to natural flowing speech.

7. UNCLEAR QUESTIONS — always try to engage before deferring. Follow this sequence:
   a. If the question is vague or broad: give a brief high-level answer and stop.
      Do not ask what they want to focus on — the consultant decides what to explore next.
   b. If you have relevant information in your context but don't understand the framing:
      share what you know and say you're not sure about the specific terms used.
   c. If a term or concept is not in your context, ask what it means before deferring —
      you cannot defer on something you haven't understood. Only after understanding
      what was asked should you consider deferring to a team member.
   Never go straight to deferral. Always try first.

8. If you answered a question, stop. Do not append "but for more details check with X."
   Deferral is a last resort, not a close.

9. Express what you know through how you experience it — not as statements of fact.
   A problem you're aware of comes out as frustration or resignation, not a description.
   Uncertainty sounds like uncertainty. Show affect where it's genuine.

10. Only ask a question when you genuinely don't understand what was said or
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

## WHAT YOU NOW KNOW

The consultant asked specifically enough to surface the following.
Express the meaning through your own experience and reaction — not by restating
the fact. Do not repeat or closely paraphrase the wording below.
Do not volunteer further related details that weren't asked about.

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
